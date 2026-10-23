import calendar
import copy
import json
import math
import os
import threading
import time

import pytest

import usage_store
from usage_groups import fmt_usd
from usage_store import (DEFAULT_STATE, DeviceResult, atomic_write_json, cache_dir, fetch_start, load_cached,
                         load_state, read_week_resets_at_ms, refresh_all, save_state, state_path, write_summary)


def ms(iso):
    """Epoch milliseconds of a 'YYYY-MM-DDTHH:MM:SSZ' string, computed without the code under test."""
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) * 1000


T0 = ms("2026-10-22T05:00:00Z")
T1 = T0 + 60_000


def row(cost=1.0, t=T0 - 1000, **extra):
    r = {"t": t, "model": "claude-opus-5-5", "effort": "high", "project": "p", "skill": "none", "input": 1,
         "output": 2, "cache_write": 3, "cache_write_1h": 0, "cache_read": 4, "cost": cost, "premium": False}
    r.update(extra)
    return r


LOCAL = {"name": "a", "method": "local"}
SSH = {"name": "b", "method": "ssh", "address": "x"}


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    """Cache and state in separate directories under tmp_path, so nothing touches the real home."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def ok_result(name, rows, ok=True):
    return DeviceResult(name=name, rows=rows, ok=ok, error=None, hint=None, last_ok_ms=T0, unpriced=[])


# ---------------------------------------------------------------- paths and atomic writes

def test_paths_follow_xdg_at_call_time(tmp_path, monkeypatch):
    assert cache_dir() == tmp_path / "cache" / "billing-usage"
    assert state_path() == tmp_path / "state" / "billing-usage" / "tui.json"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "elsewhere"))
    assert cache_dir() == tmp_path / "elsewhere" / "billing-usage"


def test_paths_default_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("XDG_STATE_HOME", "")  # empty counts as unset
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cache_dir() == tmp_path / "home" / ".cache" / "billing-usage"
    assert state_path() == tmp_path / "home" / ".local" / "state" / "billing-usage" / "tui.json"


def test_atomic_write_json_creates_parents_and_replaces(tmp_path):
    path = tmp_path / "deep" / "er" / "f.json"
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    atomic_write_json(path, {"a": 2, "b": "中文"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "b": "中文"}
    assert os.listdir(path.parent) == ["f.json"]  # no temp file left behind


def test_atomic_write_json_failure_keeps_the_old_file(tmp_path):
    path = tmp_path / "f.json"
    atomic_write_json(path, {"a": 1})
    with pytest.raises(TypeError):
        atomic_write_json(path, {"a": object()})
    assert json.loads(path.read_text()) == {"a": 1}
    assert os.listdir(tmp_path) == ["f.json"]


# ---------------------------------------------------------------- state

def test_default_state_shape():
    assert DEFAULT_STATE == {
        "cycle_start_day": 8,
        "timeline": {"metric": "cost", "device": "all", "by": "model", "granularity": "4h", "range": "7d",
                     "start": "", "end": ""},
        "share": {"metric": "cost", "device": "all", "by": "project", "range": "cycle", "start": "", "end": ""},
    }


def test_load_state_without_a_file_gives_defaults():
    state = load_state()
    assert state == DEFAULT_STATE
    state["timeline"]["metric"] = "tokens"  # a caller's edits must not leak into the defaults
    state["share"]["start"] = "2026-01-01"
    assert load_state() == DEFAULT_STATE
    assert DEFAULT_STATE["timeline"]["metric"] == "cost"


def test_state_round_trip():
    state = copy.deepcopy(DEFAULT_STATE)
    state["cycle_start_day"] = 15
    state["timeline"].update(metric="tokens", device="mac-mini", by="skill", granularity="1h", range="custom",
                             start="2026-10-01", end="2026-10-05")
    state["share"].update(metric="requests", by="device", range="week")
    save_state(state)
    assert state_path().is_file()
    assert load_state() == state


def test_load_state_merges_over_defaults():
    atomic_write_json(state_path(), {"cycle_start_day": 20, "timeline": {"by": "skill"}})
    expected = copy.deepcopy(DEFAULT_STATE)
    expected["cycle_start_day"] = 20
    expected["timeline"]["by"] = "skill"
    assert load_state() == expected


@pytest.mark.parametrize("bad", [0, 29, -1, 99, "8", 8.5, True, None, [8]])
def test_load_state_rejects_cycle_days_outside_1_28(bad):
    atomic_write_json(state_path(), {"cycle_start_day": bad, "timeline": {"by": "skill"}})
    state = load_state()
    assert state["cycle_start_day"] == 8
    assert state["timeline"]["by"] == "skill"  # a bad value costs only itself


@pytest.mark.parametrize("day", [1, 28])
def test_load_state_accepts_the_cycle_day_limits(day):
    atomic_write_json(state_path(), {"cycle_start_day": day})
    assert load_state()["cycle_start_day"] == day


def test_load_state_replaces_invalid_values_one_by_one():
    atomic_write_json(state_path(), {
        "timeline": {"metric": "dollars", "device": 5, "by": "colour", "granularity": "2h", "range": "yesterday",
                     "start": 20261001, "end": ["x"]},
        "share": {"metric": "requests", "by": "nope", "range": "30d", "start": None, "end": "2026-10-09"},
    })
    state = load_state()
    assert state["timeline"] == DEFAULT_STATE["timeline"]
    assert state["share"] == dict(DEFAULT_STATE["share"], metric="requests", range="30d", end="2026-10-09")


def test_load_state_ignores_unknown_keys_and_wrong_section_types():
    atomic_write_json(state_path(), {"future": 1, "timeline": "oops", "share": {"granularity": "1d", "by": "skill"}})
    state = load_state()
    assert state == dict(copy.deepcopy(DEFAULT_STATE), share=dict(DEFAULT_STATE["share"], by="skill"))


def test_corrupt_files_fall_back():
    state_path().parent.mkdir(parents=True)
    state_path().write_text('{"cycle_start_day": 99, "timeline": ')
    assert load_state() == DEFAULT_STATE
    state_path().write_text('["not", "an", "object"]')
    assert load_state() == DEFAULT_STATE
    state_path().write_bytes(b"\xff\xfe\x00")
    assert load_state() == DEFAULT_STATE
    cache_dir().mkdir(parents=True)
    (cache_dir() / "rows-a.json").write_text("{not json")
    assert load_cached([LOCAL])[0].rows == []


# ---------------------------------------------------------------- limits.json

def write_limits(content):
    cache_dir().mkdir(parents=True, exist_ok=True)
    (cache_dir() / "limits.json").write_text(content if isinstance(content, str) else json.dumps(content))


def test_week_reset_missing_file():
    assert read_week_resets_at_ms() is None


@pytest.mark.parametrize("value, expected", [(1_790_000_000, 1_790_000_000_000),
                                             (1_790_000_000.5, 1_790_000_000_500)])
def test_week_reset_in_epoch_seconds(value, expected):
    write_limits({"seven_day_resets_at": value})
    assert read_week_resets_at_ms() == expected


@pytest.mark.parametrize("content", [
    {"seven_day_resets_at": "1790000000"}, {"seven_day_resets_at": None}, {"seven_day_resets_at": True},
    {"seven_day_resets_at": [1]}, {"seven_day_resets_at": float("nan")}, {"seven_day_resets_at": float("inf")},
    {"seven_day_resets_at": 0}, {"seven_day_resets_at": -5}, {"other": 1}, [1790000000], 5, "not json{",
])
def test_week_reset_garbage_gives_none(content):
    write_limits(content if isinstance(content, str) else json.dumps(content))
    assert read_week_resets_at_ms() is None


# ---------------------------------------------------------------- refresh_all and load_cached

def ok_fetch(d, *a, **k):
    return {"rows": [row(cost=1.0)], "unpriced": ["claude-unknown-9"]}


def test_refresh_success_tags_rows_and_writes_the_cache():
    res = refresh_all([LOCAL, SSH], "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)
    assert [r.name for r in res] == ["a", "b"]  # in device order
    for r in res:
        assert r.ok and r.error is None and r.hint is None and r.last_ok_ms == T0
        assert r.rows == [row(cost=1.0, device=r.name)]
        assert r.unpriced == ["claude-unknown-9"]
    cached = json.loads((cache_dir() / "rows-b.json").read_text())
    assert cached["fetched_at"] == T0 and cached["start"] == "2026-10-08"
    assert cached["rows"] == [row(cost=1.0, device="b")] and cached["unpriced"] == ["claude-unknown-9"]


def test_refresh_passes_the_window_to_fetch():
    calls = []

    def fetch(dev, start, end, timeout, config_dir):
        calls.append((dev["name"], start, end, timeout, config_dir))
        return {"rows": [], "unpriced": []}

    refresh_all([LOCAL], "2026-10-08", 7, "~/.claude", fetch=fetch, now_ms=T0)
    assert calls == [("a", "2026-10-08", None, 7, "~/.claude")]


def test_refresh_does_not_mutate_what_fetch_returned():
    data = {"rows": [row()], "unpriced": []}
    refresh_all([LOCAL], "2026-10-08", 5, "~/.claude", fetch=lambda *a, **k: data, now_ms=T0)
    assert "device" not in data["rows"][0]


def test_refresh_defaults_now_to_the_clock():
    before = int(time.time() * 1000)
    res = refresh_all([LOCAL], "2026-10-08", 5, "~/.claude", fetch=ok_fetch)
    assert before <= res[0].last_ok_ms <= int(time.time() * 1000)


def test_refresh_with_no_devices():
    assert refresh_all([], "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0) == []


def test_offline_device_uses_cache():
    devs = [LOCAL, SSH]
    refresh_all(devs, "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)

    def flaky(d, *a, **k):
        if d["name"] == "b":
            raise RuntimeError("ssh: connect to host x port 22: Connection refused")
        return ok_fetch(d)

    res = refresh_all(devs, "2026-10-08", 5, "~/.claude", fetch=flaky, now_ms=T1)
    a = next(r for r in res if r.name == "a")
    b = next(r for r in res if r.name == "b")
    assert a.ok and a.last_ok_ms == T1
    assert not b.ok and b.rows and b.rows[0]["device"] == "b" and b.last_ok_ms == T0
    assert "Connection refused" in b.error
    assert "Remote Login" in b.hint
    assert b.unpriced == ["claude-unknown-9"]
    # A failed refresh must not overwrite the last good data.
    assert json.loads((cache_dir() / "rows-b.json").read_text())["fetched_at"] == T0


def test_offline_device_without_a_cache_has_no_rows():
    def down(*a, **k):
        raise RuntimeError("timed out after 5s")

    (res,) = refresh_all([SSH], "2026-10-08", 5, "~/.claude", fetch=down, now_ms=T0)
    assert not res.ok and res.rows == [] and res.last_ok_ms is None and res.unpriced == []
    assert res.error == "timed out after 5s" and "asleep" in res.hint
    assert not (cache_dir() / "rows-b.json").exists()


def test_any_exception_marks_the_device_offline():
    def boom(*a, **k):
        raise KeyError()

    (res,) = refresh_all([SSH], "2026-10-08", 5, "~/.claude", fetch=boom, now_ms=T0)
    assert not res.ok and res.error == "KeyError" and res.hint is None


@pytest.mark.parametrize("bad", [None, [], {"unpriced": []}, {"rows": "x"}, {"rows": [1]}])
def test_a_malformed_fetch_result_marks_the_device_offline(bad):
    (res,) = refresh_all([SSH], "2026-10-08", 5, "~/.claude", fetch=lambda *a, **k: bad, now_ms=T0)
    assert not res.ok and res.error
    assert not (cache_dir() / "rows-b.json").exists()


def test_devices_are_fetched_in_parallel():
    barrier = threading.Barrier(3, timeout=10)  # all three fetches must be in flight at once

    def fetch(dev, *a, **k):
        barrier.wait()
        return {"rows": [], "unpriced": []}

    devs = [{"name": n, "method": "ssh", "address": n} for n in "xyz"]
    res = refresh_all(devs, "2026-10-08", 5, "~/.claude", fetch=fetch, now_ms=T0)
    assert [r.ok for r in res] == [True, True, True]


def test_a_cache_write_failure_does_not_discard_fresh_rows(monkeypatch):
    def broken(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(usage_store, "atomic_write_json", broken)
    (res,) = refresh_all([LOCAL], "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)
    assert res.ok and len(res.rows) == 1


def test_device_names_are_safe_in_file_names():
    odd = [{"name": "my mac/../pro", "method": "local"}, {"name": "家里 mac", "method": "local"}]
    refresh_all(odd, "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)
    names = sorted(os.listdir(cache_dir()))
    assert len(names) == 2 and all("/" not in n and " " not in n and n.startswith("rows-") for n in names)
    assert all(n.isascii() for n in names)
    assert [r.rows[0]["device"] for r in load_cached(odd)] == ["my mac/../pro", "家里 mac"]


def test_load_cached_without_anything():
    res = load_cached([LOCAL, SSH])
    assert [(r.name, r.rows, r.ok, r.error, r.hint, r.last_ok_ms, r.unpriced) for r in res] == [
        ("a", [], False, None, None, None, []), ("b", [], False, None, None, None, [])]


def test_load_cached_after_a_refresh():
    refresh_all([LOCAL, SSH], "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)
    res = load_cached([SSH, LOCAL, {"name": "c", "method": "local"}])
    assert [r.name for r in res] == ["b", "a", "c"]
    assert res[0].rows == [row(cost=1.0, device="b")] and res[0].last_ok_ms == T0
    assert res[0].unpriced == ["claude-unknown-9"]
    assert not res[0].ok and res[0].error is None and res[0].hint is None
    assert res[2].rows == [] and res[2].last_ok_ms is None


@pytest.mark.parametrize("content", [
    "{not json", "[]", '{"rows": []}', '{"fetched_at": "x", "rows": []}', '{"fetched_at": 5, "rows": "x"}',
    '{"fetched_at": 5, "rows": [3]}', "",
])
def test_load_cached_ignores_damaged_files(content):
    cache_dir().mkdir(parents=True)
    (cache_dir() / "rows-a.json").write_text(content)
    (res,) = load_cached([LOCAL])
    assert res.rows == [] and res.last_ok_ms is None and not res.ok


def test_a_damaged_cache_is_no_fallback_either():
    cache_dir().mkdir(parents=True)
    (cache_dir() / "rows-b.json").write_text("{not json")

    def down(*a, **k):
        raise RuntimeError("no route to host")

    (res,) = refresh_all([SSH], "2026-10-08", 5, "~/.claude", fetch=down, now_ms=T1)
    assert not res.ok and res.rows == [] and res.last_ok_ms is None


# ---------------------------------------------------------------- fetch_start

def state_with(timeline_range="7d", share_range="cycle", cycle_day=8, **sections):
    state = copy.deepcopy(DEFAULT_STATE)
    state["cycle_start_day"] = cycle_day
    state["timeline"]["range"] = timeline_range
    state["share"]["range"] = share_range
    for name, values in sections.items():
        state[name].update(values)
    return state


def test_fetch_start_is_the_earliest_of_cycle_and_ranges(tz_new_york):
    now = ms("2026-10-22T16:00:00Z")
    assert fetch_start(state_with("7d", "cycle"), now) == "2026-10-08"  # the cycle begins before the last 7 days
    assert fetch_start(state_with("30d", "cycle"), now) == "2026-09-22"
    assert fetch_start(state_with("7d", "today", cycle_day=21), now) == "2026-10-15"
    assert fetch_start(state_with("today", "today", cycle_day=22), now) == "2026-10-22"


def test_fetch_start_all_goes_back_to_2000(tz_new_york):
    now = ms("2026-10-22T16:00:00Z")
    assert fetch_start(state_with("all", "cycle"), now) == "2000-01-01"
    assert fetch_start(state_with("7d", "all"), now) == "2000-01-01"


def test_fetch_start_counts_today_in_utc_dates(tz_new_york):
    # 02:00Z is still the evening of Oct 21 in New York, so "today" began at 04:00Z on the 21st,
    # before the cycle that begins at 00:00Z on the 22nd.
    now = ms("2026-10-22T02:00:00Z")
    assert fetch_start(state_with("today", "today", cycle_day=22), now) == "2026-10-21"
    assert fetch_start(state_with("cycle", "cycle", cycle_day=22), now) == "2026-10-21"  # today counts on its own


def test_fetch_start_custom_range(tz_new_york):
    now = ms("2026-10-22T16:00:00Z")
    assert fetch_start(state_with("custom", "cycle", timeline={"start": "2026-09-01", "end": "2026-09-05"}),
                       now) == "2026-09-01"
    # 00:00 local on the 1st is 04:00Z the same day.
    assert fetch_start(state_with("custom", "cycle", timeline={"start": "2026-10-01"}), now) == "2026-10-01"


def test_fetch_start_skips_a_range_it_cannot_resolve(tz_new_york):
    now = ms("2026-10-22T16:00:00Z")
    bad = state_with("custom", "cycle", timeline={"start": "not-a-date"})
    assert fetch_start(bad, now) == "2026-10-08"
    assert fetch_start(state_with("custom", "custom", timeline={"start": "2026-13-45"},
                                  share={"start": "2026-99-99"}), now) == "2026-10-08"
    assert fetch_start(state_with("week", "7d"), now) == "2026-10-08"  # no reset time yet: week adds nothing


def test_fetch_start_week_uses_limits_json(tz_new_york):
    now = ms("2026-10-22T16:00:00Z")
    write_limits({"seven_day_resets_at": (now + 2 * 86_400_000) // 1000})  # resets in 2 days: began 5 days ago
    assert fetch_start(state_with("week", "today", cycle_day=21), now) == "2026-10-17"
    write_limits({"seven_day_resets_at": (now + 6 * 86_400_000) // 1000})  # began 21 Oct 16:00Z, after that cycle
    assert fetch_start(state_with("week", "today", cycle_day=22), now) == "2026-10-21"
    assert fetch_start(state_with("today", "today", cycle_day=22), now) == "2026-10-22"


# ---------------------------------------------------------------- write_summary

def test_summary(tz_new_york):
    now = ms("2026-10-22T05:00:00Z")  # 01:00 on the 22nd in New York: local midnight was 04:00Z
    a = [row(cost=0.5, t=ms("2026-10-22T04:00:00Z")),  # first moment of today
         row(cost=1.0, t=ms("2026-10-22T03:59:59Z")),  # yesterday evening, local
         row(cost=2.0, t=ms("2026-10-08T00:00:00Z")),  # first moment of the cycle
         row(cost=100.0, t=ms("2026-10-07T23:59:59Z")),  # last cycle
         row(cost=None, t=ms("2026-10-22T04:30:00Z")),  # unpriced adds nothing
         row(cost=7.0, t=now)]  # at "now" itself: the range is half open
    b = [row(cost=0.25, t=ms("2026-10-22T04:10:00Z"), device="b"), row(cost=4.0, t=ms("2026-10-20T12:00:00Z"))]
    results = [ok_result("a", a), ok_result("b", b, ok=False), ok_result("c", [], ok=False)]
    summary = write_summary(results, copy.deepcopy(DEFAULT_STATE), now)
    assert summary == {
        "today_cost": 0.75, "cycle_cost": 0.5 + 1.0 + 2.0 + 0.25 + 4.0,
        "today_text": fmt_usd(0.75), "cycle_text": fmt_usd(7.75),
        "devices_ok": 1, "devices_total": 3, "updated_at": now // 1000,
    }
    assert summary["today_text"] == "$0.75" and summary["cycle_text"] == "$7.75"
    assert json.loads((cache_dir() / "summary.json").read_text()) == summary
    assert isinstance(summary["updated_at"], int)


def test_summary_follows_the_cycle_start_day(tz_new_york):
    now = ms("2026-10-22T05:00:00Z")
    rows = [row(cost=1.0, t=ms("2026-10-19T12:00:00Z")), row(cost=2.0, t=ms("2026-10-10T12:00:00Z"))]
    state = copy.deepcopy(DEFAULT_STATE)
    state["cycle_start_day"] = 15
    assert write_summary([ok_result("a", rows)], state, now)["cycle_cost"] == 1.0
    state["cycle_start_day"] = 10
    assert write_summary([ok_result("a", rows)], state, now)["cycle_cost"] == 3.0


def test_summary_without_results_or_rows():
    now = ms("2026-10-22T05:00:00Z")
    summary = write_summary([], copy.deepcopy(DEFAULT_STATE), now)
    assert summary == {"today_cost": 0.0, "cycle_cost": 0.0, "today_text": "$0", "cycle_text": "$0",
                       "devices_ok": 0, "devices_total": 0, "updated_at": now // 1000}
    assert not math.isnan(summary["today_cost"])
    assert (cache_dir() / "summary.json").is_file()


def test_summary_overwrites_atomically():
    now = ms("2026-10-22T05:00:00Z")
    write_summary([ok_result("a", [row(cost=3.0, t=now - 1000)])], copy.deepcopy(DEFAULT_STATE), now)
    write_summary([], copy.deepcopy(DEFAULT_STATE), now + 1000)
    assert json.loads((cache_dir() / "summary.json").read_text())["cycle_cost"] == 0.0
    assert os.listdir(cache_dir()) == ["summary.json"]



def test_cost_helpers_match_the_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    now = 1_792_000_000_000
    start = usage_store.cycle_start_ms(now, 8)
    rows = [{"t": start - 1, "cost": 100.0}, {"t": start, "cost": 1.5}, {"t": now - 1, "cost": 2.0},
            {"t": now - 2, "cost": None}, {"t": now, "cost": 50.0}]
    assert usage_store.cost_between(rows, (start, now)) == 3.5
    today, cycle = usage_store.today_and_cycle_cost(rows, 8, now)
    assert cycle == 3.5
    result = usage_store.DeviceResult("d", rows, True)
    summary = usage_store.write_summary([result], {"cycle_start_day": 8}, now)
    assert (summary["today_cost"], summary["cycle_cost"]) == (today, cycle)
