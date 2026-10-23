"""Pilot tests for the Textual app: header, sentences, share table, refresh, --once."""
import asyncio
import json
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
from tui_widgets import SentenceBar, share_table_lines  # noqa: E402
from usage_groups import GREY, aggregate  # noqa: E402

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
        assert "日期无效" not in screen_text(app)
        assert saved_state()["share"]["start"] == "2026-01-01" and saved_state()["share"]["end"] == today
        assert "nasa718" in screen_text(app)
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
