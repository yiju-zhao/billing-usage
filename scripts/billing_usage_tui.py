# /// script
# requires-python = ">=3.11"
# dependencies = ["textual==8.2.8"]
# ///
"""A resident terminal UI for Claude Code usage across every device in the billing-usage devices file.

Two sections, as in token-meter's sidebar: a timeline and a share table, each headed by a sentence whose words
you can change (click, or Tab then Enter). Refreshes every 60 s without any model call; `r` refreshes now,
`c` sets the billing-cycle start day, `q` quits. `--once` refreshes, writes the status-line summary, prints
one line and exits.

    uv run --script billing_usage_tui.py [--devices FILE] [--config-dir DIR] [--timeout S] [--once]
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from billing_usage import fetch_device_rows, load_devices  # noqa: E402
from usage_groups import GROUPINGS, METRICS, aggregate, fmt_usd, model_slots, segments, total, visible  # noqa: E402
from usage_store import (DeviceResult, fetch_start, load_cached, load_state, read_week_resets_at_ms,  # noqa: E402
                         refresh_all, save_state, today_and_cycle_cost, write_summary)
from usage_time import GRANULARITIES, RANGE_BUCKETS, RANGES, range_bounds  # noqa: E402

WEEK_WAITING = "（等待状态栏记录重置时间）"


def now_ms() -> int:
    return int(time.time() * 1000)


def _clock(t_ms: int, now: int) -> str:
    """HH:MM for a time today, MM/DD HH:MM otherwise (local time)."""
    t, n = time.localtime(t_ms / 1000), time.localtime(now / 1000)
    same_day = (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday)
    return time.strftime("%H:%M" if same_day else "%m/%d %H:%M", t)


def offline_line(result: DeviceResult, now: int) -> str:
    """`<name> 离线 · 最后成功于 HH:MM · <hint>`, or `· 从未成功` when it was never read."""
    when = "最后成功于 " + _clock(result.last_ok_ms, now) if result.last_ok_ms is not None else "从未成功"
    reason = result.hint or result.error
    return "%s 离线 · %s" % (result.name, when) + (" · " + reason if reason else "")


def granularity_label(label: str) -> str:
    """`每 4 小时`, and `每天` / `每小时` / `每分钟` for a single unit."""
    return "每" + label[2:] if label.startswith("1 ") else "每 " + label


# ---------------------------------------------------------------- --once

def run_once(devices: List[dict], config_dir: str, timeout: int) -> int:
    state = load_state()
    results = refresh_all(devices, fetch_start(state, now_ms()), timeout, config_dir, fetch=fetch_device_rows)
    now = now_ms()
    try:
        summary = write_summary(results, state, now)
        cycle_text, today_text = summary["cycle_text"], summary["today_text"]
    except OSError:  # no summary file, but the line can still be printed
        today, cycle = today_and_cycle_cost([r for res in results for r in res.rows], state["cycle_start_day"], now)
        cycle_text, today_text = fmt_usd(cycle), fmt_usd(today)
    ok = sum(1 for r in results if r.ok)
    print("本周期 %s · 今日 %s · %d/%d 台在线" % (cycle_text, today_text, ok, len(results)))
    for result in results:
        if not result.ok:
            print(offline_line(result, now))
    return 0


# ---------------------------------------------------------------- the app

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal  # noqa: E402
from textual.widgets import Rule, Static  # noqa: E402

from tui_widgets import (ALERT, BG, BRIGHT, MUTED, RULE, TEXT, WARN, CycleDayScreen, DateRange,  # noqa: E402
                         SentenceBar, ShareTable, TimelineChart, fmt_metric)

SECTIONS = ("timeline", "share")


class UsageApp(App):
    TITLE = "Billing Usage"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None  # no word lit up at start; Tab reaches the first one
    BINDINGS = [
        Binding("r", "refresh_now", "刷新", show=False),
        Binding("q", "quit", "退出", show=False),
        Binding("c", "cycle_day", "周期", show=False),
    ]
    CSS = f"""
    Screen {{ background: {BG}; color: {TEXT}; layout: vertical; }}
    /* the rule above reaches the dialogs too; keep the sections visible behind them */
    Screen.-dialog {{ background: {BG} 0%; }}
    Screen.-dialog-dim {{ background: {BG} 55%; }}
    #top {{ height: 1; padding: 0 1; }}
    #title {{ width: 1fr; height: 1; overflow: hidden hidden; }}
    #keys {{ width: auto; padding-left: 2; }}
    #alerts {{ height: auto; max-height: 6; padding: 0 1; display: none; }}
    #alerts.shown {{ display: block; }}
    Rule.-horizontal {{ color: {RULE}; margin: 0; height: 1; }}
    #timeline {{ height: 1fr; min-height: 1; }}
    #share-table {{ height: 1fr; min-height: 2; }}
    """

    def __init__(self, devices: List[dict], *, config_dir: str, timeout: int = 120,
                 fetch: Callable = fetch_device_rows, refresh_s: float = 60) -> None:
        super().__init__()
        self.devices, self.config_dir, self.timeout = devices, config_dir, timeout
        self.fetch, self.refresh_s = fetch, refresh_s
        self.state = load_state()
        names = {d["name"] for d in devices}
        for section in SECTIONS:
            if self.state[section]["device"] != "all" and self.state[section]["device"] not in names:
                self.state[section]["device"] = "all"
        self.results: List[DeviceResult] = load_cached(devices)
        self._slots = self._compute_slots()
        self._refreshing = False
        self._refreshed = False  # a refresh has finished at least once
        self._last_refresh_ms: Optional[int] = None
        self._fetched_from: Optional[str] = None  # the start date the rows on hand were fetched from
        self._refresh_error: Optional[str] = None  # why the latest refresh failed as a whole; None after a good one
        # 自定义日期 picked but no valid dates entered yet: the word and the date row show, the data keeps the
        # previous range (state[section]["range"] is only set to "custom" once a pair is entered).
        self._pending_custom = {section: False for section in SECTIONS}

    # ------------------------------------------------------------ layout

    @staticmethod
    def _range_options():
        """RANGES, with "week" disabled and explained until the status line has recorded the reset time."""
        waiting = read_week_resets_at_ms() is None
        return [(k, label, WEEK_WAITING) if k == "week" and waiting else (k, label) for k, label in RANGES]

    def _fields(self, section: str):
        fields = [
            ("metric", list(METRICS.items())),
            ("device", [("all", "所有设备")] + [(d["name"], d["name"]) for d in self.devices]),
            ("by", list(GROUPINGS.items())),
        ]
        if section == "timeline":
            fields.append(("granularity", [(k, granularity_label(label)) for k, label, _ in GRANULARITIES]))
        fields.append(("range", self._range_options()))
        return fields

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="title")
            yield Static(self._keys_text(), id="keys")
        yield Static(id="alerts")
        for section in SECTIONS:
            yield Rule(line_style="solid")
            yield SentenceBar(section, self._fields(section), values=self.state[section], id=f"{section}-sentence")
            s = self.state[section]
            dates = DateRange(section, s["start"], s["end"], id=f"{section}-dates")
            dates.set_class(s["range"] == "custom", "shown")
            yield dates
            yield TimelineChart(id="timeline") if section == "timeline" else ShareTable(id="share-table")

    @staticmethod
    def _title_text(parts: list, named: bool) -> Text:
        title = Text()
        if named:
            title.append("Billing Usage", style=f"bold {BRIGHT}")
            title.append("   ")
        for i, part in enumerate(parts):
            if i:
                title.append(" · ", style=MUTED)
            title.append(part[0], style=MUTED)
            title.append(part[1], style=BRIGHT)
            if len(part) > 2:
                title.append(part[2], style=MUTED)
        return title

    @staticmethod
    def _keys_text() -> Text:
        text = Text()
        text.append("r", style=BRIGHT)
        text.append(" 刷新  ", style=MUTED)
        text.append("q", style=BRIGHT)
        text.append(" 退出", style=MUTED)
        return text

    def on_mount(self) -> None:
        self.render_all()
        self.start_refresh()
        self.set_interval(self.refresh_s, self._tick)
        self.set_interval(15, self._render_header)

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_header)

    # ------------------------------------------------------------ data

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    def _all_rows(self) -> List[dict]:
        return [r for result in self.results for r in result.rows]

    def _compute_slots(self):
        return model_slots(sorted({str(r["model"]) for r in self._all_rows()}))

    def _bounds(self, section: str, now: int) -> Tuple[Optional[Tuple[int, int]], str]:
        """The section's (from, to), or None with the reason to show instead of data."""
        s = self.state[section]
        try:
            bounds = range_bounds(s["range"], now, cycle_day=self.state["cycle_start_day"],
                                  week_resets_at_ms=read_week_resets_at_ms(), custom_start=s["start"],
                                  custom_end=s["end"])
        except ValueError:
            return None, "日期无效"
        if bounds is None:
            return None, "等待状态栏记录重置时间"
        if bounds[1] < bounds[0]:
            return None, "日期无效"
        return bounds, ""

    def _section_rows(self, section: str, now: int) -> Tuple[List[dict], Optional[Tuple[int, int]], str]:
        bounds, why = self._bounds(section, now)
        if bounds is None:
            return [], None, why
        device = self.state[section]["device"]
        first, last = bounds
        rows = [r for r in self._all_rows() if first <= r["t"] < last and (device == "all" or r["device"] == device)]
        return rows, bounds, "无数据"

    # ------------------------------------------------------------ rendering

    def render_all(self) -> None:
        now = now_ms()
        shown = {}
        ranges = self._range_options()
        for section in SECTIONS:
            rows, bounds, empty = self._section_rows(section, now)
            shown[section] = rows
            s = self.state[section]
            metric, by = s["metric"], s["by"]
            whole = total(rows, metric)
            bar = self.query_one(f"#{section}-sentence", SentenceBar)
            bar.set_options("range", ranges)
            bar.set_total(fmt_metric(whole, metric))
            if section == "timeline":
                picked = next(ms for k, _, ms in GRANULARITIES if k == s["granularity"])
                first, last = bounds if bounds else (0, 0)
                self.query_one(TimelineChart).update(rows, by, metric, picked, first, last, empty=empty,
                                                     slots=self._slots)
            else:
                groups = visible(aggregate(rows, by, self._slots), metric, whole)
                other = [g for g in segments(rows, by, metric, self._slots) if g.other]
                grey = other[0].member_keys if other else ()
                self.query_one(ShareTable).update(groups, metric, by, whole, grey=grey, empty=empty)
        self._render_header(shown)

    def _render_header(self, shown: Optional[dict] = None) -> None:
        now = now_ms()
        if shown is None:
            shown = {section: self._section_rows(section, now)[0] for section in SECTIONS}
        today, cycle = today_and_cycle_cost(self._all_rows(), self.state["cycle_start_day"], now)
        parts = [("今日 ", fmt_metric(today, "cost")), ("本周期 ", fmt_metric(cycle, "cost"))]
        ok = sum(1 for r in self.results if r.ok)
        if self._refreshed:
            parts.append(("", "%d/%d" % (ok, len(self.results)), " 台在线"))
        else:
            parts.append(("", "%d" % len(self.results), " 台设备"))
        if self._refreshing:
            parts.append(("刷新中…", ""))
        elif self._last_refresh_ms is not None:
            minutes = (now - self._last_refresh_ms) // 60_000
            parts.append(("刚刚刷新" if minutes < 1 else "%d 分钟前刷新" % minutes, ""))
        title_box = self.query_one("#title", Static)
        room = title_box.size.width or 10_000
        # On a narrow terminal drop whole parts rather than cut one in half: the refresh age first, then the
        # name, then the online count; the two costs stay.
        choices = [(parts, True), (parts[:3], True), (parts[:3], False), (parts[:2], False)]
        for kept, named in choices:
            title = self._title_text(kept, named)
            if title.cell_len <= room:
                break
        title_box.update(title)

        alerts = []
        if self._refresh_error is not None:
            alerts.append(Text("刷新失败：%s" % self._refresh_error, style=ALERT))
        for result in self.results:
            if not result.ok and result.error is not None:
                alerts.append(Text(offline_line(result, now), style=ALERT))
        seen = {}
        for section_rows in shown.values():
            for r in section_rows:
                seen[id(r)] = r
        unpriced = sorted({str(r["model"]) for r in seen.values() if r["cost"] is None})
        if unpriced:
            alerts.append(Text("未定价：%s（tokens 计入，花费按 0）" % "、".join(unpriced), style=WARN))
        premium = sum(1 for r in seen.values() if r.get("premium"))
        if premium:
            alerts.append(Text("%s 次请求用了快速模式或仅美国推理（花费偏低）" % f"{premium:,}", style=WARN))
        box = self.query_one("#alerts", Static)
        box.update(Text("\n").join(alerts))
        box.set_class(bool(alerts), "shown")

    # ------------------------------------------------------------ refresh

    def start_refresh(self) -> bool:
        """Start a refresh in a background thread unless one is running. True when one was started."""
        if self._refreshing:  # only the event loop's thread starts and ends refreshes, so no lock is needed
            return False
        self._refreshing = True
        start = fetch_start(self.state, now_ms())
        # A plain daemon thread, not a Textual worker: those run on the event loop's default executor, which
        # asyncio waits for at exit, so quitting during a slow ssh device would hang until --timeout.
        threading.Thread(target=self._refresh_thread, args=(start,), daemon=True, name="refresh").start()
        self._render_header()
        return True

    def _refresh_thread(self, start: str) -> None:
        error = None
        try:
            results = refresh_all(self.devices, start, self.timeout, self.config_dir, fetch=self.fetch)
        except Exception as e:  # refresh_all handles each device; anything else keeps the old data
            results, error = None, str(e) or type(e).__name__
        try:
            self.call_from_thread(self._refresh_done, results, start, error)
        except RuntimeError:
            pass  # the app has gone

    def _refresh_done(self, results: Optional[List[DeviceResult]], start: str, error: Optional[str] = None) -> None:
        self._refreshing = False
        if results is None:
            # Keep the old rows, age and summary.json as they were and say so; the next tick retries.
            self._refresh_error = error or "未知错误"
            self.render_all()
            return
        self.results = results
        self._slots = self._compute_slots()
        self._fetched_from = start
        self._refresh_error = None
        self._refreshed = True
        now = now_ms()
        self._last_refresh_ms = now
        try:
            write_summary(self.results, self.state, now)
        except OSError:
            pass
        self.render_all()
        if fetch_start(self.state, now_ms()) < start:  # a choice made meanwhile needs older rows
            self.start_refresh()

    def _tick(self) -> None:
        if not self._refreshing:
            self.start_refresh()

    def action_refresh_now(self) -> None:
        self.start_refresh()

    def _refetch_if_needed(self) -> None:
        """Fetch again when the choices now reach further back than the rows on hand."""
        if self._refreshing:
            return  # _refresh_done checks once the running refresh is in
        # Nothing fetched yet means the refreshes so far failed; the timer retries those, not every choice.
        if self._fetched_from is not None and fetch_start(self.state, now_ms()) < self._fetched_from:
            self.start_refresh()

    # ------------------------------------------------------------ choices

    def _save(self) -> None:
        try:
            save_state(self.state)
        except OSError:
            pass

    def _custom_dates_ok(self, start: str, end: str) -> bool:
        """A start date is given and the pair makes a range that is not inverted."""
        if not start:
            return False
        try:
            bounds = range_bounds("custom", now_ms(), cycle_day=self.state["cycle_start_day"],
                                  week_resets_at_ms=None, custom_start=start, custom_end=end)
        except ValueError:
            return False
        return bounds is not None and bounds[0] <= bounds[1]

    def on_sentence_bar_changed(self, message: SentenceBar.Changed) -> None:
        section, field, value = message.section, message.field, message.value
        s = self.state[section]
        bar = self.query_one(f"#{section}-sentence", SentenceBar)
        bar.set_value(field, value)
        if field == "range":
            dates = self.query_one(f"#{section}-dates", DateRange)
            dates.set_class(value == "custom", "shown")
            self._pending_custom[section] = False
            dates.set_message("")
            if value == "custom":
                dates.focus_start()
                if s["range"] != "custom" and not self._custom_dates_ok(s["start"], s["end"]):
                    self._pending_custom[section] = True  # keep showing the previous range until dates are in
                    dates.set_message("输入起止日期后回车")
            elif section == "timeline" and value in RANGE_BUCKETS:
                s["granularity"] = RANGE_BUCKETS[value]
                bar.set_value("granularity", s["granularity"])
        if not self._pending_custom[section] or field != "range":
            s[field] = value
        self._save()
        self.render_all()
        self._refetch_if_needed()

    def on_date_range_submitted(self, message: DateRange.Submitted) -> None:
        section = message.section
        dates = self.query_one(f"#{section}-dates", DateRange)
        if not self._custom_dates_ok(message.start, message.end):
            dates.set_message("日期无效", error=True)
            return
        dates.set_message("")
        self._pending_custom[section] = False
        self.state[section]["range"] = "custom"
        self.state[section]["start"], self.state[section]["end"] = message.start, message.end
        self._save()
        self.render_all()
        self._refetch_if_needed()

    def action_cycle_day(self) -> None:
        def chosen(day: Optional[int]) -> None:
            if day is None:
                return
            self.state["cycle_start_day"] = day
            self._save()
            self.render_all()
            self._refetch_if_needed()

        self.push_screen(CycleDayScreen(self.state["cycle_start_day"]), chosen)


# ---------------------------------------------------------------- entry point

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--devices", help="devices file (default: ~/.config/billing-usage/devices.json, else this machine)")
    p.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    p.add_argument("--timeout", type=int, default=120, help="seconds to wait for each ssh device (default: 120)")
    p.add_argument("--once", action="store_true", help="refresh, write the status-line summary, print one line, exit")
    a = p.parse_args(argv)
    devices = load_devices(a.devices)
    if a.once:
        return run_once(devices, a.config_dir, a.timeout)
    app = UsageApp(devices, config_dir=a.config_dir, timeout=a.timeout)
    app.run()
    code = app.return_code or 0
    if app.refreshing:  # a slow device is still being read; its threads would hold the exit until --timeout
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
