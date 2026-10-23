"""Pilot tests for the Textual app: header, sentences, share table, refresh, --once."""
import asyncio
import bisect
import json
import re
import threading
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("textual")

import usage_store  # noqa: E402
from billing_usage_tui import UsageApp, main  # noqa: E402
from conftest import assistant, write_transcript  # noqa: E402
from textual.widgets import Input, OptionList  # noqa: E402
from rich.cells import cell_len  # noqa: E402
from tui_timeline import BAND, Legend, Plot, TimelineChart, bucket_title  # noqa: E402
from tui_widgets import BG, SentenceBar, ShareTable, fmt_metric, share_table_lines  # noqa: E402
from usage_groups import GREY, aggregate  # noqa: E402
from usage_time import GRANULARITIES  # noqa: E402
from billing_usage_tui import granularity_label  # noqa: E402

NOW = int(time.time() * 1000)
DEVS = [{"name": "a", "method": "local"}, {"name": "b", "method": "ssh", "address": "b.example"}]
REFUSED = "ssh: connect to host b.example port 22: Connection refused"


def row(t, project, cost=1.0, model="claude-opus-5-5", effort="high", skill="none", premium=False, tokens=1000):
    return {"t": t, "model": model, "effort": effort, "project": project, "skill": skill, "input": tokens,
            "output": 0, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0, "cost": cost,
            "premium": premium}


def fake_fetch(dev, start, end, timeout, config_dir):
    if dev["name"] == "a":
        return {"rows": [row(NOW - 1000, "nasa718", 3.0), row(NOW - 2000, "career-ops", 1.0)], "unpriced": []}
    return {"rows": [row(NOW - 3000, "nasa718", 2.0)], "unpriced": []}


def failing_b(dev, start, end, timeout, config_dir):
    if dev["name"] == "b":
        raise RuntimeError(REFUSED)
    return fake_fetch(dev, start, end, timeout, config_dir)


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    for name in ("XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    return tmp_path


def screen_text(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def settle(app, pilot, timeout=5.0):
    """Wait until no refresh is in flight and the screen has caught up."""
    deadline = time.monotonic() + timeout
    await pilot.pause()
    while app.refreshing:
        assert time.monotonic() < deadline, "refresh did not finish"
        await pilot.pause(0.01)
    await pilot.pause()


def run(app, body, size=(120, 40)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await body(pilot)
    asyncio.run(go())


def saved_state():
    path = usage_store.state_path()
    return json.loads(path.read_text()) if path.exists() else None


# ---------------------------------------------------------------- header and share table

def test_header_and_share_table():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        text = screen_text(app)
        assert "Billing Usage" in text
        assert "今日 $6 · 本周期 $6 · 2/2 台在线 · 刚刚刷新" in text
        assert "r 刷新" in text and "q 退出" in text
        assert "nasa718" in text and "career-ops" in text
        lines = [ln for ln in text.splitlines() if "nasa718" in ln]
        assert lines and "83%" in lines[0] and "$5" in lines[0]
        assert "花费 · 所有设备 · 按项目 · 本计费周期" in text
        assert "花费 · 所有设备 · 按模型 · 每 4 小时 · 最近 7 天" in text
        assert "离线" not in text
    run(app, body)


def test_summary_written_after_refresh():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        summary = json.loads((usage_store.cache_dir() / "summary.json").read_text())
        assert summary["cycle_text"] == "$6" and summary["devices_ok"] == 2 and summary["devices_total"] == 2
        assert summary["updated_at"] >= NOW // 1000
    run(app, body)


def test_offline_line_never_succeeded():
    app = UsageApp(DEVS, config_dir="x", fetch=failing_b)

    async def body(pilot):
        text = screen_text(app)
        assert "1/2 台在线" in text and "本周期 $4" in text
        assert "b 离线 · 从未成功 · no ssh server is listening" in text
        assert "nasa718" in text
    run(app, body)


def test_offline_device_keeps_cached_rows_and_says_when():
    usage_store.refresh_all(DEVS, "2026-01-01", 5, "x", fetch=fake_fetch, now_ms=NOW)
    app = UsageApp(DEVS, config_dir="x", fetch=failing_b)

    async def body(pilot):
        text = screen_text(app)
        hhmm = time.strftime("%H:%M", time.localtime(NOW / 1000))
        assert f"b 离线 · 最后成功于 {hhmm} · no ssh server is listening" in text
        assert "本周期 $6" in text and "1/2 台在线" in text
    run(app, body)


def test_unpriced_and_premium_warnings():
    def fetch(dev, start, end, timeout, config_dir):
        return {"rows": [row(NOW - 1000, "p", None, model="claude-zeta-9"),
                         row(NOW - 2000, "p", 1.0, premium=True)], "unpriced": ["claude-zeta-9"]}
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fetch)

    async def body(pilot):
        text = screen_text(app)
        assert "未定价：claude-zeta-9（tokens 计入，花费按 0）" in text
        assert "1 次请求用了快速模式或仅美国推理（花费偏低）" in text
    run(app, body)


def test_models_needing_no_price_are_not_warned_about():
    def fetch(dev, start, end, timeout, config_dir):
        return {"rows": [row(NOW - 1000, "p", None, model="claude-opus-4-7"),
                         row(NOW - 2000, "p", 1.0)], "unpriced": ["claude-opus-4-7"]}
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fetch)

    async def body(pilot):
        assert "未定价" not in screen_text(app)
    run(app, body)


def test_empty_range_shows_no_data():
    app = UsageApp(DEVS, config_dir="x", fetch=lambda *a: {"rows": [], "unpriced": []})

    async def body(pilot):
        text = screen_text(app)
        assert "无数据" in text and "本周期 $0" in text
    run(app, body)


def test_narrow_terminal_then_resize():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        assert "nasa718" in screen_text(app)
        app.post_message(SentenceBar.Changed("share", "by", "model"))
        await pilot.pause()
        text = screen_text(app)
        assert "opus-5-5 high" in text
        await pilot.resize_terminal(140, 50)
        text = screen_text(app)
        assert "输入/输出 $/M" in text and "4/20" in text and "0.2/8" in text
    run(app, body, size=(70, 20))


# ---------------------------------------------------------------- sentences

def test_choice_persists():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("share", "by", "skill"))
        await pilot.pause()
        assert saved_state()["share"]["by"] == "skill"
        assert "花费 · 所有设备 · 按 skill · 本计费周期" in screen_text(app)
        assert "无 skill" in screen_text(app)
    run(app, body)
    assert usage_store.load_state()["share"]["by"] == "skill"
    again = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body2(pilot):
        assert "按 skill · 本计费周期" in screen_text(again)
    run(again, body2)


def test_range_sets_granularity_but_custom_keeps_it():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("timeline", "range", "today"))
        await settle(app, pilot)
        assert saved_state()["timeline"]["granularity"] == "30m"
        assert "每 30 分钟 · 今天" in screen_text(app)
        app.post_message(SentenceBar.Changed("timeline", "range", "custom"))
        await settle(app, pilot)
        assert saved_state()["timeline"]["granularity"] == "30m"
        app.post_message(SentenceBar.Changed("timeline", "range", "30d"))
        await settle(app, pilot)
        assert "每天 · 最近 30 天" in screen_text(app)
    run(app, body)


def test_device_filter_and_metric():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("share", "device", "b"))
        await pilot.pause()
        text = screen_text(app)
        assert "花费 · b · 按项目" in text and "career-ops" not in text
        app.post_message(SentenceBar.Changed("share", "metric", "requests"))
        await pilot.pause()
        assert "请求数 · b · 按项目" in screen_text(app)
        assert saved_state()["share"]["device"] == "b"
    run(app, body)


def test_keyboard_opens_choices_and_week_is_disabled_without_reset():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        bar = app.query_one("#share-sentence", SentenceBar)
        bar.word("range").focus()
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        week = options.get_option("week")
        assert week.disabled
        assert "（等待状态栏记录重置时间）" in screen_text(app)
        options.highlighted = options.get_option_index("7d")
        await pilot.press("enter")
        await settle(app, pilot)
        assert saved_state()["share"]["range"] == "7d"
        assert "按项目 · 最近 7 天" in screen_text(app)
    run(app, body)


def test_week_enabled_once_reset_is_known():
    path = usage_store.cache_dir() / "limits.json"
    usage_store.atomic_write_json(path, {"seven_day_resets_at": NOW // 1000 + 3600})
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        bar = app.query_one("#share-sentence", SentenceBar)
        bar.word("range").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not app.screen.query_one(OptionList).get_option("week").disabled
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        await pilot.click(bar.word("range"))
        await pilot.pause()
        assert app.screen is not app.screen_stack[0]
        await pilot.click(offset=(2, 0))  # outside the list closes it, nothing chosen
        await pilot.pause()
        assert app.screen is app.screen_stack[0] and saved_state() is None
    run(app, body)


def test_tab_reaches_the_words_and_click_opens_choices():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        await pilot.press("tab")
        assert app.focused is app.query_one("#timeline-sentence", SentenceBar).word("metric")
        await pilot.click(app.query_one("#share-sentence", SentenceBar).word("by"))
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("device")
        await pilot.press("enter")
        await pilot.pause()
        assert saved_state()["share"]["by"] == "device"
    run(app, body)


def test_custom_dates_validate_and_save():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)
    today = time.strftime("%Y-%m-%d", time.localtime(NOW / 1000))

    async def body(pilot):
        app.post_message(SentenceBar.Changed("share", "range", "custom"))
        await settle(app, pilot)
        # nothing typed yet: a neutral prompt, and the previous range (本计费周期) still drives the data
        text = screen_text(app)
        assert "输入起止日期后回车" in text and "日期无效" not in text
        assert "花费 · 所有设备 · 按项目 · 自定义日期" in text and "nasa718" in text and "$6" in text
        assert saved_state() is None or saved_state()["share"]["range"] == "cycle"
        start, end = app.query("#share-dates Input").results(Input)
        start.value, end.value = "2026-13-01", ""
        start.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "日期无效" in screen_text(app)
        assert saved_state()["share"]["start"] == ""
        start.value, end.value = today, "2026-01-01"  # inverted
        await pilot.press("enter")
        await pilot.pause()
        assert "日期无效" in screen_text(app)
        start.value, end.value = "2026-01-01", today
        await pilot.press("enter")
        await settle(app, pilot)
        text = screen_text(app)
        assert "日期无效" not in text and "输入起止日期后回车" not in text
        saved = saved_state()["share"]
        assert saved["range"] == "custom" and saved["start"] == "2026-01-01" and saved["end"] == today
        assert "nasa718" in screen_text(app)
        # leave and come back: the entered dates apply at once, with no prompt
        app.post_message(SentenceBar.Changed("share", "range", "7d"))
        await settle(app, pilot)
        app.post_message(SentenceBar.Changed("share", "range", "custom"))
        await settle(app, pilot)
        text = screen_text(app)
        assert "输入起止日期后回车" not in text and "日期无效" not in text
        assert saved_state()["share"]["range"] == "custom"
    run(app, body)


def test_custom_pick_then_back_without_dates_keeps_state_clean():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("timeline", "range", "custom"))
        await settle(app, pilot)
        assert "输入起止日期后回车" in screen_text(app)
        start = app.query("#timeline-dates Input").results(Input).__next__()
        start.value = ""
        start.focus()
        await pilot.press("enter")  # submitting nothing is an actual invalid pair
        await pilot.pause()
        assert "日期无效" in screen_text(app)
        app.post_message(SentenceBar.Changed("timeline", "range", "today"))
        await settle(app, pilot)
        text = screen_text(app)
        assert "日期无效" not in text and "输入起止日期后回车" not in text
        assert saved_state()["timeline"]["range"] == "today"
    run(app, body)


def test_cycle_day_dialog():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        await pilot.press("c")
        await pilot.pause()
        field = app.screen.query_one(Input)
        field.value = "30"
        await pilot.press("enter")
        await pilot.pause()
        assert "1–28" in screen_text(app)
        field.value = "15"
        await pilot.press("enter")
        await settle(app, pilot)
        assert saved_state()["cycle_start_day"] == 15
        assert app.screen is app.screen_stack[0]
    run(app, body)


# ---------------------------------------------------------------- refresh

def test_refresh_never_overlaps_and_refetches_for_an_earlier_range():
    gate = threading.Event()
    calls = []

    def slow(dev, start, end, timeout, config_dir):
        calls.append((dev["name"], start))
        gate.wait(5)
        return fake_fetch(dev, start, end, timeout, config_dir)

    app = UsageApp(DEVS, config_dir="x", fetch=slow)

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.05)
            assert app.refreshing and "刷新中…" in screen_text(app)
            await pilot.press("r")
            app.action_refresh_now()
            app._tick()
            app.post_message(SentenceBar.Changed("share", "range", "all"))
            await pilot.pause(0.05)
            assert len(calls) == 2  # one refresh, one call per device
            gate.set()
            await settle(app, pilot)
            # the "all" range needed an earlier start than the first fetch covered
            assert len(calls) == 4 and calls[-1][1] == usage_store.ALL_TIME_START
    asyncio.run(go())


def test_timer_refreshes():
    calls = []

    def counting(dev, start, end, timeout, config_dir):
        calls.append(dev["name"])
        return fake_fetch(dev, start, end, timeout, config_dir)

    app = UsageApp(DEVS, config_dir="x", fetch=counting, refresh_s=0.2)

    async def body(pilot):
        await pilot.pause(0.5)
        await settle(app, pilot)
        assert len(calls) >= 4
    run(app, body)


# ---------------------------------------------------------------- share table rendering

def test_share_table_greys_rows_merged_into_other():
    rows = [row(NOW - i, "p%d" % i, cost=10.0 - i) for i in range(8)]
    groups = aggregate(rows, "project")
    head, lines = share_table_lines(groups, "cost", "project", sum(10.0 - i for i in range(8)),
                                    grey={"p6", "p7"}, width=100)
    assert head.plain.split()[:2] == ["项目", "占比"]
    by_name = {ln.plain.split()[0]: ln for ln in lines}
    assert set(by_name) == {"p%d" % i for i in range(8)}
    bar_style = [str(span.style) for span in by_name["p7"].spans if "█" in by_name["p7"].plain[span.start:span.end]]
    assert bar_style and all(GREY in s for s in bar_style)
    assert all(GREY not in str(span.style) for span in by_name["p0"].spans)
    assert all(len(ln.plain) <= 100 for ln in lines)


def test_share_table_bar_is_18_cells_and_sorted_by_metric():
    rows = [row(NOW, "big", cost=1.0, tokens=10), row(NOW, "small", cost=3.0, tokens=1)]
    groups = aggregate(rows, "project")
    _, lines = share_table_lines(groups, "tokens", "project", 11, grey=(), width=100)
    assert lines[0].plain.split()[0] == "big"
    bar = lines[0].plain.split()[1]
    assert len(bar) == 18 and set(bar) <= {"█", "░"}


def test_share_table_narrow_drops_price_columns():
    groups = aggregate([row(NOW, "p")], "model")
    head, lines = share_table_lines(groups, "cost", "model", 1.0, grey=(), width=60)
    assert "输入/输出" not in head.plain
    assert all(cell_len(ln.plain) <= 60 for ln in [head] + lines)


# ---------------------------------------------------------------- --once

def _iso(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_once_cli(tmp_path, capsys):
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0}
    write_transcript(tmp_path / "claude" / "projects" / "-p",
                     [assistant(_iso(NOW - 60_000), "m1", "r1", "claude-opus-5-5", usage, cwd="/x/proj")])
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"devices": [{"name": "here", "method": "local"}]}))
    main(["--once", "--devices", str(devices), "--config-dir", str(tmp_path / "claude")])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "本周期 $4 · 今日 $4 · 1/1 台在线"
    summary = json.loads((usage_store.cache_dir() / "summary.json").read_text())
    assert summary["cycle_text"] == "$4" and summary["devices_ok"] == 1


def test_once_cli_lists_offline_devices(tmp_path, capsys, monkeypatch):
    import billing_usage_tui
    monkeypatch.setattr(billing_usage_tui, "fetch_device_rows", failing_b)
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"devices": DEVS}))
    main(["--once", "--devices", str(devices)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "本周期 $4 · 今日 $4 · 1/2 台在线"
    assert lines[1] == "b 离线 · 从未成功 · no ssh server is listening: turn on sshd there " \
                       "(macOS: System Settings > General > Sharing > Remote Login)"


def test_refresh_age_and_week_option_follow_time_and_limits():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app._last_refresh_ms = int(time.time() * 1000) - 5 * 60_000 - 1000
        app._render_header()
        await pilot.pause()
        assert "5 分钟前刷新" in screen_text(app)
        usage_store.atomic_write_json(usage_store.cache_dir() / "limits.json",
                                      {"seven_day_resets_at": NOW // 1000 + 3600})
        await pilot.press("r")
        await settle(app, pilot)
        assert "刚刚刷新" in screen_text(app)
        bar = app.query_one("#timeline-sentence", SentenceBar)
        bar.word("range").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not app.screen.query_one(OptionList).get_option("week").disabled
    run(app, body)



def test_failed_refresh_keeps_old_data_age_and_summary(monkeypatch):
    import billing_usage_tui
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)
    summary_path = usage_store.cache_dir() / "summary.json"
    calls = []
    real_refresh_all = billing_usage_tui.refresh_all

    def broken(*args, **kwargs):
        calls.append(args[1])
        raise RuntimeError("disk on fire")

    async def body(pilot):
        before = summary_path.read_text()
        mtime = summary_path.stat().st_mtime_ns
        app._last_refresh_ms = int(time.time() * 1000) - 5 * 60_000 - 1000
        monkeypatch.setattr(billing_usage_tui, "refresh_all", broken)
        await pilot.press("r")
        await settle(app, pilot)
        text = screen_text(app)
        assert "刷新失败：disk on fire" in text
        assert "5 分钟前刷新" in text and "刚刚刷新" not in text
        assert "本周期 $6" in text and "2/2 台在线" in text and "nasa718" in text
        assert summary_path.read_text() == before and summary_path.stat().st_mtime_ns == mtime
        # a choice that needs no older rows does not set off another attempt; the timer retries
        app.post_message(SentenceBar.Changed("share", "by", "skill"))
        await settle(app, pilot)
        assert len(calls) == 1
        app._tick()
        await settle(app, pilot)
        assert len(calls) == 2
        monkeypatch.setattr(billing_usage_tui, "refresh_all", real_refresh_all)
        await pilot.press("r")
        await settle(app, pilot)
        text = screen_text(app)
        assert "刷新失败" not in text and "刚刚刷新" in text
        assert json.loads(summary_path.read_text())["updated_at"] >= json.loads(before)["updated_at"]
    run(app, body)


def test_first_refresh_failing_does_not_refresh_on_every_choice(monkeypatch):
    import billing_usage_tui
    usage_store.refresh_all(DEVS, "2026-01-01", 5, "x", fetch=fake_fetch, now_ms=NOW)  # a cache to show
    calls = []

    def broken(*args, **kwargs):
        calls.append(args[1])
        raise RuntimeError("boom")

    monkeypatch.setattr(billing_usage_tui, "refresh_all", broken)
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        assert "刷新失败：boom" in screen_text(app) and "本周期 $6" in screen_text(app)
        assert not (usage_store.cache_dir() / "summary.json").exists()
        for by in ("skill", "device", "model"):
            app.post_message(SentenceBar.Changed("share", "by", by))
            await settle(app, pilot)
        assert len(calls) == 1
    run(app, body)



def test_failed_earlier_start_refresh_is_not_retried_by_every_choice(monkeypatch):
    import billing_usage_tui
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)
    calls = []

    def broken(*args, **kwargs):
        calls.append(args[1])
        raise RuntimeError("boom")

    async def body(pilot):
        monkeypatch.setattr(billing_usage_tui, "refresh_all", broken)
        app.post_message(SentenceBar.Changed("share", "range", "all"))  # needs an earlier start
        await settle(app, pilot)
        assert calls == [usage_store.ALL_TIME_START]
        assert "刷新失败：boom" in screen_text(app)
        for field, value in (("by", "skill"), ("device", "a"), ("by", "model")):
            app.post_message(SentenceBar.Changed("share", field, value))
            await settle(app, pilot)
        assert len(calls) == 1
        app._tick()
        await settle(app, pilot)
        assert len(calls) == 2
    run(app, body)


# ---------------------------------------------------------------- the timeline

HOUR = 3_600_000
OPUS_HIGH, OPUS_XHIGH, SONNET_HIGH = "#3987e5", "#6da7ec", "#d95926"
BAR_GLYPHS = set("█▁▂▃▄▅▆▇")


def chart_rows():
    return [row(NOW - 2 * HOUR, "nasa718", 4.0), row(NOW - 2 * HOUR, "career-ops", 2.0, model="claude-sonnet-5-5"),
            row(NOW - 30 * HOUR, "nasa718", 3.0, effort="xhigh"),
            row(NOW - 54 * HOUR, "aps", 1.0, model="claude-sonnet-5-5")]


def chart_fetch(dev, start, end, timeout, config_dir):
    if dev["name"] == "a":
        return {"rows": chart_rows(), "unpriced": []}
    return {"rows": [row(NOW - 3 * HOUR, "aps", 1.0, model="claude-sonnet-5-5")], "unpriced": []}


def cells_of(app, widget):
    """(char, fg, bg) for every cell of `widget` on screen, row by row."""
    strips = app.screen._compositor.render_strips()
    r = widget.region
    grid = []
    for y in range(r.y, r.bottom):
        line = []
        for seg in strips[y].crop(r.x, r.right):
            st = seg.style
            fg = st.color.triplet.hex if st and st.color and st.color.triplet else None
            bg = st.bgcolor.triplet.hex if st and st.bgcolor and st.bgcolor.triplet else None
            line.extend((ch, fg, bg) for ch in seg.text)
        grid.append(line)
    return grid


def widget_text(app, widget):
    return "\n".join("".join(ch for ch, _, _ in line) for line in cells_of(app, widget))


def bar_colours(app):
    """The colours the bars are drawn in (the panel and highlight backgrounds left out)."""
    out = set()
    for line in cells_of(app, app.query_one(Plot)):
        for ch, fg, bg in line:
            if ch in BAR_GLYPHS:
                out.add(fg)
                if ch != "█" and bg:
                    out.add(bg)
    return out - {BG, BAND, None}


def test_timeline_draws_stacked_bars_axis_labels_total_and_legend():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        assert bar_colours(app) == {OPUS_HIGH, OPUS_XHIGH, SONNET_HIGH}
        text = widget_text(app, app.query_one(Plot))
        lines = text.splitlines()
        assert "$10" in lines[0].split() and "+/- 缩放" in lines[0]
        assert any("┤" in ln for ln in lines) and any("└" in ln for ln in lines)
        assert time.strftime("%m/%d", time.localtime(NOW / 1000)) in text  # the x axis is labelled in dates
        legend = widget_text(app, app.query_one(Legend))
        assert "■ opus-5-5 high 40%" in legend and "■ sonnet-5-5 high 30%" in legend
        assert "■ opus-5-5 xhigh 30%" in legend
    run(app, body)


def test_plus_zooms_in_and_the_sentence_shows_the_finer_bucket():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        axis = chart.data.axis
        assert chart.window is None and chart.data.g == 4 * HOUR
        await pilot.press("+")
        await pilot.pause()
        assert chart.window is not None
        assert chart.window[1] - chart.window[0] < axis[1] - axis[0]
        g = chart.data.g
        assert g < 4 * HOUR
        label = granularity_label(next(lb for _, lb, ms in GRANULARITIES if ms == g))
        assert "· %s · 最近 7 天" % label in screen_text(app)
        assert app.state["timeline"]["granularity"] == "4h"  # the choice itself is unchanged
        await pilot.press("minus")
        await pilot.pause()
        assert chart.window is None and "· 每 4 小时 · 最近 7 天" in screen_text(app)
    run(app, body)


def test_left_after_zoom_pans_the_window():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        await pilot.press("+")
        await pilot.pause()
        before = chart.window
        await pilot.press("left")
        await pilot.pause()
        after = chart.window
        span = before[1] - before[0]
        assert after[1] - after[0] == span and after[0] == before[0] - span // 4
        await pilot.press("right")
        await pilot.pause()
        assert chart.window == before
        await pilot.press("shift+left")  # moves the highlight even while zoomed
        await pilot.pause()
        assert chart.hl_ms is not None and chart.window == before
        app.post_message(SentenceBar.Changed("timeline", "range", "30d"))  # a new range starts over
        await pilot.pause()
        assert chart.window is None and chart.hl_ms is None and "· 每天 · 最近 30 天" in screen_text(app)
    run(app, body)


def test_isolating_a_legend_entry_leaves_only_its_colour_and_total():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        await pilot.press("down")  # the first legend entry
        await pilot.pause()
        assert chart.legend_key == "claude-opus-5-5\thigh"
        assert OPUS_HIGH in bar_colours(app) and SONNET_HIGH not in bar_colours(app)  # the others are dimmed
        await pilot.press("i")
        await pilot.pause()
        assert chart.only == "claude-opus-5-5\thigh"
        assert bar_colours(app) == {OPUS_HIGH}
        top = widget_text(app, app.query_one(Plot)).splitlines()[0].split()
        assert "$4" in top and "$10" not in top
        await pilot.press("i")  # again: back to every segment
        await pilot.pause()
        assert chart.only is None and bar_colours(app) >= {OPUS_HIGH}
        assert "$10" in widget_text(app, app.query_one(Plot)).splitlines()[0].split()
    run(app, body)


def test_switching_grouping_clears_only_and_a_vanished_segment_clears_it():
    app = UsageApp(DEVS, config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        await pilot.press("down", "i")
        await pilot.pause()
        assert chart.only == "claude-opus-5-5\thigh"
        app.post_message(SentenceBar.Changed("timeline", "by", "project"))
        await pilot.pause()
        assert chart.only is None and chart.legend_key is None
        await pilot.press("down", "i")
        await pilot.pause()
        assert chart.only == "nasa718"
        app.post_message(SentenceBar.Changed("timeline", "device", "b"))  # b never worked on nasa718
        await pilot.pause()
        assert chart.only is None
        assert bar_colours(app)  # the chart is drawn again
    run(app, body)


def detail_lines_on(text):
    lines = text.splitlines()
    opus = [i for i, ln in enumerate(lines) if re.search(r"opus-5-5 high\s+\$4\s+67%", ln)]
    sonnet = [i for i, ln in enumerate(lines) if re.search(r"sonnet-5-5 high\s+\$2\s+33%", ln)]
    total = [i for i, ln in enumerate(lines) if re.search(r"合计\s+\$6\s+│", ln)]  # no share before the border
    return opus, sonnet, total


async def highlight_bucket(pilot, chart, t_ms):
    """Highlight the bucket holding `t_ms` with the keyboard: the first ← lights the latest bar."""
    starts = chart.data.starts
    target = bisect.bisect_right(starts, t_ms) - 1
    for _ in range(len(starts) - target):
        await pilot.press("left")
    await pilot.pause()
    return target


def test_highlighted_bar_lists_its_segments_largest_first_and_a_total_without_share():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        assert chart.hl_ms is None and "合计" not in screen_text(app)  # nothing highlighted at first
        starts = chart.data.starts
        target = await highlight_bucket(pilot, chart, NOW - 2 * HOUR)
        assert chart.hl_index() == target
        text = screen_text(app)
        opus, sonnet, total = detail_lines_on(text)
        assert opus and sonnet and total and opus[0] < sonnet[0] < total[0]
        assert bucket_title(starts[target], chart.data.ends[target], chart.data.g) in text
        await pilot.press("left")
        await pilot.pause()
        assert chart.hl_index() == max(0, target - 1)
        await pilot.press("escape")
        await pilot.pause()
        assert chart.hl_ms is None and "合计" not in screen_text(app)
    run(app, body)


def test_detail_box_sits_inside_the_plot_beside_the_bar_on_a_narrow_screen():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        await highlight_bucket(pilot, chart, NOW - 2 * HOUR)
        plot = app.query_one(Plot)
        text = widget_text(app, plot)
        opus, sonnet, total = detail_lines_on(text)
        assert opus and sonnet and total and opus[0] < sonnet[0] < total[0]
        assert "╭" in text and "╰" in text
        assert app.query_one(ShareTable).region.height >= 2
        hit = plot.hit
        lines = text.splitlines()
        x0 = hit.gutter + hit.xs[chart.hl_index() - hit.first]
        box_cols = [ln.index("╭") for ln in lines if "╭" in ln]
        assert box_cols and (box_cols[0] > x0 or box_cols[0] + 20 < x0)  # beside the bar, not over it
    run(app, body, size=(70, 24))


def test_hover_highlights_a_bar_and_clicking_a_legend_entry_picks_it():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        plot = app.query_one(Plot)
        hit = plot.hit
        await pilot.hover(Plot, offset=(hit.gutter + hit.xs[0] + hit.bar - 1, hit.top))
        await pilot.pause()
        assert chart.hl_ms is not None and chart.hl_index() == hit.first
        legend = app.query_one(Legend)
        await pilot.hover(Legend, offset=(0, 0))
        await pilot.pause()
        assert chart.hl_ms is None  # a highlight that followed the mouse goes with it
        y, x0, _, key = legend.hits[1]
        await pilot.click(Legend, offset=(x0 + 1, y))
        await pilot.pause()
        assert chart.legend_key == key
        await pilot.click(Legend, offset=(x0 + 1, y))
        await pilot.pause()
        assert chart.legend_key is None
    run(app, body)


def test_mouse_wheel_zooms_around_the_bar_under_the_mouse():
    from textual import events
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        plot, chart = app.query_one(Plot), app.query_one(TimelineChart)
        hit = plot.hit
        x, y = hit.gutter + hit.xs[2], hit.top + 1
        under = chart.data.starts[hit.first + 2]
        plot.post_message(events.MouseScrollUp(plot, x, y, 0, 1, 0, False, False, False, screen_x=x, screen_y=y))
        await pilot.pause()
        await pilot.pause()
        assert chart.window is not None and chart.window[0] <= under < chart.window[1]
        assert chart.data.starts[chart.hl_index()] >= under
        plot.post_message(events.MouseScrollDown(plot, x, y, 0, -1, 0, False, False, False, screen_x=x, screen_y=y))
        await pilot.pause()
        await pilot.pause()
        assert chart.window is None
    run(app, body)


def test_100x30_keeps_chart_legend_detail_and_share_table_on_screen():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=chart_fetch)

    async def body(pilot):
        await pilot.press("left")
        await pilot.pause()
        text = screen_text(app)
        assert bar_colours(app)
        assert "■ opus-5-5 high" in text and "合计" in text
        table = app.query_one(ShareTable)
        assert table.region.height >= 4 and table.region.bottom <= 30
        assert "项目" in text and re.search(r"nasa718\s+\S+\s+70%", text)
        assert all(cell_len(ln) <= 100 for ln in text.splitlines())
        title = text.splitlines()[0]
        assert "今日" in title and "r 刷新" in title
    run(app, body, size=(100, 30))


# ---------------------------------------------------------------- bars across the whole plot

def even_fetch(dev, start, end, timeout, config_dir):
    """The same cost in every 4-hour bucket of the last week, so every bar is full height."""
    return {"rows": [row(NOW - k * 4 * HOUR - 1000, "nasa718", 1.0) for k in range(7 * 6)], "unpriced": []}


def bar_runs(line, plot_x0):
    """(start, length) of each run of bar cells on one plot row, columns from `plot_x0`."""
    runs, start = [], None
    for x, (ch, _, _) in enumerate(line + [(" ", None, None)]):
        if ch in BAR_GLYPHS and start is None:
            start = x
        elif ch not in BAR_GLYPHS and start is not None:
            runs.append((start - plot_x0, x - start))
            start = None
    return runs


@pytest.mark.parametrize("size", [(120, 40), (160, 40), (100, 30)])
def test_bars_spread_over_the_whole_plot_in_one_width(size):
    app = UsageApp(DEVS[:1], config_dir="x", fetch=even_fetch)

    async def body(pilot):
        chart, plot = app.query_one(TimelineChart), app.query_one(Plot)
        hit = plot.hit
        n, cols = len(chart.data.starts), plot.size.width - hit.gutter
        assert hit.count == n and hit.width == cols
        bottom = cells_of(app, plot)[hit.bottom - 3]  # the lowest bar row, above the axis and its labels
        runs = bar_runs(bottom, hit.gutter)
        assert runs[0][0] == 0 and cols - (runs[-1][0] + runs[-1][1]) <= 2  # from the axis to the right edge
        if cols / n >= 3:
            assert len(runs) == n and {length for _, length in runs} == {hit.bar}  # apart, one width
            assert [x for x, _ in runs] == list(hit.xs)
        else:
            assert sum(length for _, length in runs) == n * hit.bar
        # the total sits over the middle of the plot, not of the bars' left part
        top = widget_text(app, plot).splitlines()[0]
        text = fmt_metric(chart.data.whole, "cost")
        mid = top.index(" " + text + " ") + 1 + len(text) / 2
        assert abs(mid - (hit.gutter + cols / 2)) <= 1
        # the mouse finds a bar's bucket on every one of its columns
        for j, x in enumerate(hit.xs):
            for c in range(x, x + hit.bar):
                assert plot._bucket_at(hit.gutter + c, hit.top) == hit.first + j
        assert plot._bucket_at(hit.gutter - 1, hit.top) is None
        assert plot._bucket_at(hit.gutter + cols, hit.top) is None
    run(app, body, size=size)


# ---------------------------------------------------------------- keys while typing dates

def test_q_quits_while_a_custom_date_input_has_focus():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("timeline", "range", "custom"))
        await settle(app, pilot)
        assert isinstance(app.focused, Input)
        await pilot.press("x", "2", "-")  # only digits and dashes type into a date
        assert app.focused.value == "2-"
        await pilot.press("q")
        await pilot.pause()
        assert app.return_code == 0  # the app is exiting
    run(app, body)


def test_entering_dates_hands_focus_back_so_r_refreshes():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fake_fetch)
    today = time.strftime("%Y-%m-%d", time.localtime(NOW / 1000))

    async def body(pilot):
        calls = []
        app.action_refresh_now = lambda: calls.append(1)
        app.post_message(SentenceBar.Changed("timeline", "range", "custom"))
        await settle(app, pilot)
        start, end = app.query("#timeline-dates Input").results(Input)
        assert app.focused is start
        await pilot.press(*"2026-01-01")
        end.value = today
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.state["timeline"]["range"] == "custom" and app.state["timeline"]["start"] == "2026-01-01"
        assert app.focused is app.query_one("#timeline-sentence", SentenceBar).word("range")
        await pilot.press("r")
        await pilot.pause()
        assert calls == [1]
    run(app, body)


def test_escape_in_a_date_input_lets_go_of_it():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        app.post_message(SentenceBar.Changed("share", "range", "custom"))
        await settle(app, pilot)
        assert isinstance(app.focused, Input)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.focused, Input)
        assert app.focused is app.query_one("#share-sentence", SentenceBar).word("range")
    run(app, body)


def test_keys_inside_the_cycle_day_dialog():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fake_fetch)

    async def body(pilot):
        await pilot.press("c")
        await pilot.pause()
        field = app.screen.query_one(Input)
        await pilot.press("c")  # no second dialog over the first
        await pilot.pause()
        assert len(app.screen_stack) == 2 and app.screen.query_one(Input) is field
        await pilot.press("backspace", "x", "1", "2")
        assert field.value == "12"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.state["cycle_start_day"] == 12 and len(app.screen_stack) == 1
    run(app, body)


# ---------------------------------------------------------------- share table focus

def test_tab_skips_the_share_table_but_the_wheel_still_scrolls_it():
    from textual import events
    from textual.containers import VerticalScroll

    def many(dev, start, end, timeout, config_dir):
        return {"rows": [row(NOW - 1000 - i, "p%02d" % i, 1.0 + i) for i in range(30)], "unpriced": []}
    app = UsageApp(DEVS[:1], config_dir="x", fetch=many)

    async def body(pilot):
        app.query_one("#share-sentence", SentenceBar).word("range").focus()
        await pilot.press("tab")
        assert not isinstance(app.focused, VerticalScroll)
        assert app.focused is app.query_one("#timeline-sentence", SentenceBar).word("metric")
        scroll = app.query_one(ShareTable).query_one(VerticalScroll)
        assert scroll.max_scroll_y > 0
        x, y = 2, 1
        scroll.post_message(events.MouseScrollDown(scroll, x, y, 0, 1, 0, False, False, False,
                                                   screen_x=x, screen_y=y))
        await pilot.pause()
        await pilot.pause()
        assert scroll.scroll_y > 0
    run(app, body, size=(100, 30))


# ---------------------------------------------------------------- cycle day and summary.json

def test_a_new_cycle_day_rewrites_the_summary():
    def spread(dev, start, end, timeout, config_dir):
        return {"rows": [row(NOW - k * 24 * HOUR - 1000, "p", 1.0) for k in range(40)], "unpriced": []}
    app = UsageApp(DEVS[:1], config_dir="x", fetch=spread)
    path = usage_store.cache_dir() / "summary.json"

    async def body(pilot):
        app._refetch_if_needed = lambda: None  # so only the choice itself can write the summary
        before = json.loads(path.read_text())["cycle_cost"]
        day = next(d for d in range(1, 29) if usage_store.today_and_cycle_cost(
            app._all_rows(), d, NOW)[1] != before)
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one(Input).value = str(day)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.state["cycle_start_day"] == day
        assert json.loads(path.read_text())["cycle_cost"] != before
    run(app, body)


def test_a_new_cycle_day_leaves_the_summary_alone_after_a_failed_refresh():
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fake_fetch)
    path = usage_store.cache_dir() / "summary.json"

    async def body(pilot):
        app._refresh_error = "boom"
        mtime = path.stat().st_mtime_ns
        await pilot.press("c")
        await pilot.pause()
        app.screen.query_one(Input).value = "3"
        await pilot.press("enter")
        await pilot.pause()
        assert app.state["cycle_start_day"] == 3 and path.stat().st_mtime_ns == mtime
    run(app, body)


# ---------------------------------------------------------------- legend order

def test_legend_is_largest_first_while_the_bars_keep_their_stack_order():
    def fetch(dev, start, end, timeout, config_dir):
        return {"rows": [row(NOW - 2 * HOUR, "p", 1.0), row(NOW - 2 * HOUR, "p", 3.0, effort="xhigh"),
                         row(NOW - 2 * HOUR, "p", 6.0, model="claude-sonnet-5-5")], "unpriced": []}
    app = UsageApp(DEVS[:1], config_dir="x", fetch=fetch)
    opus, xhigh, sonnet = "claude-opus-5-5\thigh", "claude-opus-5-5\txhigh", "claude-sonnet-5-5\thigh"

    async def body(pilot):
        chart = app.query_one(TimelineChart)
        assert [s.key for s in chart.data.segs] == [opus, xhigh, sonnet]  # stack order, bottom up
        legend = app.query_one(Legend)
        assert [key for _, _, _, key in legend.hits] == [sonnet, xhigh, opus]
        text = widget_text(app, legend)
        assert text.index("sonnet-5-5 high 60%") < text.index("opus-5-5 xhigh 30%") < text.index("opus-5-5 high 10%")
        grid = cells_of(app, app.query_one(Plot))
        hit = app.query_one(Plot).hit
        col = hit.gutter + hit.xs[bisect.bisect_right(chart.data.starts, NOW - 2 * HOUR) - 1 - hit.first]
        assert grid[hit.bottom - 3][col][1] == OPUS_HIGH  # the bar's bottom is still opus high
        picked = []
        for _ in range(3):
            await pilot.press("down")
            picked.append(chart.legend_key)
        assert picked == [sonnet, xhigh, opus]
        await pilot.press("up")
        assert chart.legend_key == xhigh
    run(app, body)
