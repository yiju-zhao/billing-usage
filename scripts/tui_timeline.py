"""The timeline section: one stacked bar per bucket with the total centred above and the legend wrapping under
it. The highlighted bar's breakdown is a box drawn beside it over the plot, as token-meter's tooltip, so it never
takes rows from the share table.

Keys, bound on the app: +/- zoom around the highlighted bar (the latest one when none is); ←/→ highlight a bar
and move the highlight, or pan a quarter of the window once zoomed (shift+←/→ still move the highlight then);
↑/↓ pick a legend entry, which dims the other segments; i shows only the picked segment, and again shows them
all; Esc lets go of the bar and the legend entry. With the mouse: hovering a bar highlights it until the mouse
leaves, the wheel zooms, hovering or clicking a legend entry picks it.

What to draw is decided by pure functions of rows and numbers (render_bars, build_chart, bar_geometry,
x_labels, zoom_window, pan_window, bucket_detail, bucket_title, legend_lines); the widgets only lay them out.
"""
from __future__ import annotations

import bisect
import math
import time
from datetime import date
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from tui_widgets import BG, BRIGHT, MUTED, RULE, SURFACE, TEXT, _fit, _pad, blend, fmt_metric
from usage_groups import OTHER_KEY, Group, fmt_pct, row_tokens, segments, total
from usage_time import DAY_MS, MIN_SPAN_MS, MINUTE_MS, bucket_start, next_bucket_start, zoom_granularity

Cell = Tuple[str, Optional[str], Optional[str]]  # (glyph, fg, bg)
BLANK: Cell = (" ", None, None)
BLOCKS = " ▁▂▃▄▅▆▇█"  # BLOCKS[n]: the lower n eighths of a cell
SWATCH = "■"  # one cell wide in JetBrainsMono Nerd Font (unlike ▰▱, which fall back to another font)
DIM_MIX = 0.6  # a segment that is not the picked one fades this far towards the panel background
BAND = "#23252b"  # behind the highlighted bar
HL_AXIS = "#9a9ea6"  # the axis under the highlighted bar
BOX_EDGE = "#3a3d45"  # the breakdown box's border, as the dropdowns'
LABEL_GAP = 2  # cells between two x-axis labels
LEGEND_GAP = 3
LEGEND_LABEL_MAX = 24
VALUE_W, PCT_W = 7, 5  # the breakdown's value and share columns ("$0.0123", "<0.1%")
NAME_MIN, NAME_MAX = 4, 16
PLOT_MIN = 6  # the plot's rows the legend leaves it: total, 3 bar rows, axis, labels
# Key hints on the chart's top line, in the order they are dropped from the end when the line is short.
HINTS = [("+/-", "缩放"), ("←/→", "移动"), ("i", "只看此段"), ("↑/↓", "选段")]


# ---------------------------------------------------------------- bar cells

def _cell(pieces: List[Tuple[Optional[str], int]]) -> Cell:
    """One cell from its bottom-up (colour, eighths) pieces, which add up to 8 with None for the empty top.
    A glyph has two colours, so with more than one boundary the segment holding most of the cell takes the
    eighths of the pieces between it and the one piece that stays (the empty top when the stack ends here)."""
    if len(pieces) == 1:
        return BLANK if pieces[0][0] is None else ("█", pieces[0][0], None)
    if len(pieces) == 2:
        (low, n), (high, _) = pieces
        return BLOCKS[n], low, high
    top_color, top_n = pieces[-1]
    if top_color is None:  # the stack ends in this cell: keep its height
        biggest = max(pieces[:-1], key=lambda p: p[1])  # the first, so the lowest, on a tie
        return BLOCKS[8 - top_n], biggest[0], None
    i = max(range(len(pieces)), key=lambda k: pieces[k][1])
    biggest = pieces[i][0]
    bottom_color, bottom_n = pieces[0]
    if i == 0 or (i < len(pieces) - 1 and top_n >= bottom_n):
        return BLOCKS[8 - top_n], biggest, top_color
    return BLOCKS[bottom_n], bottom_color, biggest


def _column(stack: Sequence[Tuple[float, str]], height: int, vmax: float) -> List[Cell]:
    """One stack's cells, bottom first. Running totals are rounded to eighths, so heights never go down."""
    if not vmax or vmax <= 0:
        return [BLANK] * height
    eighths = height * 8
    spans: List[List] = []  # [colour, low, high) in eighths; neighbours of one colour are one span
    running, edge = 0.0, 0
    for value, color in stack:
        running += max(0.0, value or 0.0)
        top = min(eighths, math.floor(running / vmax * eighths + 0.5))
        if top > edge:
            if spans and spans[-1][0] == color:
                spans[-1][2] = top
            else:
                spans.append([color, edge, top])
            edge = top
    cells = []
    for r in range(height):
        low, high = 8 * r, 8 * r + 8
        pieces = [(c, min(high, b) - max(low, a)) for c, a, b in spans if a < high and b > low]
        filled = sum(n for _, n in pieces)
        if filled < 8:
            pieces.append((None, 8 - filled))
        cells.append(_cell(pieces))
    return cells


def render_bars(stacks: List[List[Tuple[float, str]]], height: int, vmax: float) -> List[List[Cell]]:
    """`height` rows (top first) of one `(glyph, fg, bg)` cell per stack; each stack is bottom-up
    `(value, colour)` pairs scaled so `vmax` fills the column. A cell holding one boundary is the lower-block
    glyph of the lower segment's eighths, fg the lower colour and bg the upper one (None above the stack)."""
    if height <= 0:
        return []
    columns = [_column(stack, height, vmax) for stack in stacks]
    return [[col[height - 1 - r] for col in columns] for r in range(height)]


def nice_max(v: float) -> float:
    """The y axis's top: `v` rounded up to 1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6 or 8 times a power of ten."""
    if v <= 0:
        return 0
    power = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * power >= v * (1 - 1e-9):
            return m * power
    return 10 * power


# ---------------------------------------------------------------- buckets

def bucket_starts(start_ms: int, end_ms: int, g_ms: int) -> List[int]:
    """The starts of the `g_ms` buckets that cover `[start_ms, end_ms)`, as bucket_start/next_bucket_start
    count them (from each local midnight), so a clock change shortens a bucket instead of shifting the axis."""
    out: List[int] = []
    t = bucket_start(start_ms, g_ms)
    while t < end_ms:
        midnight = next_bucket_start(t, DAY_MS)
        if g_ms >= DAY_MS:
            out.append(t)
            t = midnight
            continue
        while t < midnight and t < end_ms:  # next_bucket_start, without a time lookup per bucket
            out.append(t)
            t += g_ms
        if t >= midnight:
            t = midnight
    return out


def metric_value(r: dict, metric: str) -> float:
    if metric == "cost":
        return r["cost"] or 0.0
    if metric == "tokens":
        return float(row_tokens(r))
    return 1.0


def segment_key(r: dict, by: str) -> str:
    """The Group.key that aggregate() files the row under."""
    if by == "model":
        return "%s\t%s" % (str(r["model"]), r["effort"])
    return r.get(by) or ("none" if by == "skill" else "unknown")


@dataclass
class ChartData:
    segs: List[Group]  # what is drawn, bottom-up, as segments() gives them over the whole range
    whole: float  # total() over every row of the range, dropped specks included
    axis: Optional[Tuple[int, int]]  # the first used bucket to the end of the bucket holding the range's end
    window: Optional[Tuple[int, int]]  # the zoomed window inside the axis, None when not zoomed
    g: int  # bucket size on screen
    starts: List[int] = field(default_factory=list)
    ends: List[int] = field(default_factory=list)
    values: Dict[int, Dict[str, float]] = field(default_factory=dict)  # bucket -> segment key -> value
    totals: Dict[int, float] = field(default_factory=dict)  # bucket -> every row in it, specks included


def build_chart(rows: List[dict], by: str, metric: str, picked_ms: int, range_to: int,
                slots: Optional[dict], window: Optional[Tuple[int, int]]) -> ChartData:
    """Bucket the rows per segment. Rows of groups merged into 其他 count as 其他; rows of specks too small to
    be a segment only count in the bucket totals. Zoomed, the bucket size follows zoom_granularity."""
    segs = segments(rows, by, metric, slots)
    whole = total(rows, metric)
    if not rows:
        return ChartData(segs, whole, None, None, picked_ms)
    first = min(r["t"] for r in rows)
    last = max(max(r["t"] for r in rows), range_to - 1)
    axis = (bucket_start(first, picked_ms), next_bucket_start(bucket_start(last, picked_ms), picked_ms))
    full = axis[1] - axis[0]
    if window is not None:
        window = (max(axis[0], window[0]), min(axis[1], window[1]))
        if window[1] - window[0] >= full or window[1] <= window[0]:
            window = None
    g = zoom_granularity(picked_ms, full, window[1] - window[0]) if window else picked_ms
    starts = bucket_starts(*(window or axis), g)
    ends = starts[1:] + [next_bucket_start(starts[-1], g)] if starts else []
    data = ChartData(segs, whole, axis, window, g, starts, ends)
    if not starts:
        return data
    keys = {s.key for s in segs}
    merged = {k for s in segs if s.other for k in s.member_keys}
    lo, hi = starts[0], ends[-1]
    for r in rows:
        t = r["t"]
        if not lo <= t < hi:
            continue
        i = bisect.bisect_right(starts, t) - 1
        v = metric_value(r, metric)
        data.totals[i] = data.totals.get(i, 0.0) + v
        key = segment_key(r, by)
        key = OTHER_KEY if key in merged else key
        if key in keys:
            bucket = data.values.setdefault(i, {})
            bucket[key] = bucket.get(key, 0.0) + v
    return data


# ---------------------------------------------------------------- geometry, labels, zoom

def bar_geometry(n: int, cols: int, hl: int) -> Tuple[int, int, int, int]:
    """`(first, count, slot, bar)`: the leftmost bucket shown, how many show, the columns each one gets and
    the bar's width in them (the rest is a gap on its left). Fewer buckets than columns: ⌊cols/n⌋ each, with a
    1-column gap when that leaves a bar 2 or more wide. More: one column each, the latest ones showing unless
    the highlight `hl` is further back."""
    if n <= 0 or cols <= 0:
        return 0, 0, 0, 0
    if n <= cols:
        slot = cols // n
        return 0, n, slot, slot - 1 if slot - 1 >= 2 else slot
    first = min(n - cols, max(0, hl))
    return first, cols, 1, 1


def _midnight(t_ms: int) -> int:
    return bucket_start(t_ms, DAY_MS)


def x_labels(starts: List[int], end_ms: int, first: int, count: int, slot: int, bar: int, g: int,
             width: int) -> List[Tuple[int, str]]:
    """`(column, text)` labels under the bars shown: dates (`10/16`) at midnights when the view spans two days
    or more or the buckets are days, `HH:MM` at round times otherwise (a midnight still shows its date). The
    step is the finest that keeps two cells between labels."""
    if count <= 0 or slot <= 0:
        return []
    shown = starts[first:first + count]
    span_end = starts[first + count] if first + count < len(starts) else end_ms
    offset = slot - bar
    per_bucket = slot  # columns per bucket
    need = 5 + LABEL_GAP
    candidates: List[Tuple[int, str]] = []
    if g >= DAY_MS or span_end - shown[0] >= 2 * DAY_MS:
        per_day = per_bucket * max(1.0, DAY_MS / g)
        step = next((k for k in (1, 2, 3, 7, 14) if k * per_day >= need), None)
        months = next((m for m in (1, 2, 3, 6, 12) if m * 30 * per_day >= need), 12)
        for k, s in enumerate(shown):
            if _midnight(s) != s:
                continue
            day = time.localtime(s / 1000)
            if step is not None:
                keep = date(day.tm_year, day.tm_mon, day.tm_mday).toordinal() % step == 0
            else:
                keep = day.tm_mday == 1 and (day.tm_mon - 1) % months == 0
            if keep:
                candidates.append((k * slot + offset, time.strftime("%m/%d", day)))
    else:
        steps = [m * MINUTE_MS for m in (1, 2, 5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440)]
        step = next((s for s in steps if s >= g and s % g == 0 and s / g * per_bucket >= need), DAY_MS)
        for k, s in enumerate(shown):
            m = _midnight(s)
            if (s - m) % step == 0:
                text = time.strftime("%m/%d" if s == m else "%H:%M", time.localtime(s / 1000))
                candidates.append((k * slot + offset, text))
    labels: List[Tuple[int, str]] = []
    for col, text in candidates:
        if col + cell_len(text) > width:
            continue
        if labels and col < labels[-1][0] + cell_len(labels[-1][1]) + LABEL_GAP:
            continue
        labels.append((col, text))
    if not labels and cell_len(time.strftime("%m/%d", time.localtime(shown[0] / 1000))) <= width:
        labels.append((offset, time.strftime("%m/%d", time.localtime(shown[0] / 1000))))
    return labels


def zoom_window(window: Optional[Tuple[int, int]], axis: Tuple[int, int], anchor_ms: int,
                factor: float) -> Optional[Tuple[int, int]]:
    """The window `factor` times as wide (under 1 zooms in), keeping `anchor_ms` where it is on screen; never
    narrower than MIN_SPAN_MS, and None (the whole axis) once it would be as wide as the axis."""
    a0, a1 = axis
    full = a1 - a0
    if full <= MIN_SPAN_MS:
        return None
    w0, w1 = window or axis
    span = w1 - w0
    new = max(MIN_SPAN_MS, math.floor(span * factor + 0.5))
    if new >= full:
        return None
    anchor = min(max(anchor_ms, w0), w1)
    start = math.floor(anchor - (anchor - w0) / span * new + 0.5)
    start = min(max(start, a0), a1 - new)
    return start, start + new


def pan_window(window: Tuple[int, int], axis: Tuple[int, int], direction: int) -> Tuple[int, int]:
    """The window moved a quarter of its width (direction -1 earlier, 1 later), stopping at the axis's ends."""
    span = window[1] - window[0]
    start = window[0] + direction * (span // 4)
    start = min(max(start, axis[0]), axis[1] - span)
    return start, start + span


def bucket_detail(values: Dict[str, float], segs: Sequence[Group]) -> List[Tuple[Group, float]]:
    """The bucket's non-zero segments, largest first."""
    items = [(s, values.get(s.key, 0.0)) for s in segs]
    return sorted([(s, v) for s, v in items if v > 0], key=lambda item: -item[1])


def bucket_title(start: int, end: int, g: int) -> str:
    """`10/16` for a day, `10/16 08:03` for a minute, `10/16 08:00–12:00` otherwise (24:00 at midnight)."""
    day = time.strftime("%m/%d", time.localtime(start / 1000))
    if g >= DAY_MS:
        return day
    begin = time.strftime("%H:%M", time.localtime(start / 1000))
    if g <= MINUTE_MS:
        return "%s %s" % (day, begin)
    finish = "24:00" if _midnight(end) == end else time.strftime("%H:%M", time.localtime(end / 1000))
    return "%s %s–%s" % (day, begin, finish)


def legend_lines(entries: Sequence[Tuple[str, int]], width: int, gap: int = LEGEND_GAP) -> List[List[Tuple[str, int]]]:
    """`(key, cells)` entries wrapped into lines of `(key, x)`; an entry wider than the line gets one alone."""
    lines: List[List[Tuple[str, int]]] = []
    x = 0
    for key, w in entries:
        if lines and x + gap + w <= width:
            lines[-1].append((key, x + gap))
            x += gap + w
        else:
            lines.append([(key, 0)])
            x = w
    return lines


def fit_legend(entries: Sequence[Tuple[str, int, float]], width: int, max_lines: int
               ) -> Tuple[List[List[Tuple[str, int]]], int]:
    """legend_lines() for `(key, cells, value)` entries in at most `max_lines` lines. When they do not all fit,
    the biggest values that fit together with a `+N` stay, still in their own order; returns the lines and N."""
    lines = legend_lines([(k, w) for k, w, _ in entries], width)
    if len(lines) <= max_lines:
        return lines, 0
    kept: List[int] = []
    for i in sorted(range(len(entries)), key=lambda i: -entries[i][2]):
        trial = sorted(kept + [i])
        more = ("+", len("+%d" % (len(entries) - len(trial))))
        if len(legend_lines([entries[j][:2] for j in trial] + [more], width)) <= max_lines:
            kept = trial
    return legend_lines([entries[j][:2] for j in kept], width), len(entries) - len(kept)


# ---------------------------------------------------------------- widgets


class Hit(NamedTuple):
    """Where the plot drew its bars, for the mouse."""
    top: int  # first bar row
    bottom: int  # one past the x-label row
    gutter: int  # columns left of the bars
    first: int
    count: int
    slot: int


def _style(fg: Optional[str], bg: Optional[str]) -> str:
    return " ".join(p for p in (fg or "", ("on " + bg) if bg else "") if p)


class Plot(Widget):
    """The total line, the y axis, the bars, the x axis and its labels, and the highlighted bar's breakdown in
    a box beside it."""

    def __init__(self, chart: "TimelineChart") -> None:
        super().__init__()
        self.chart = chart
        self.hit: Optional[Hit] = None

    def render(self) -> Text:
        return Text("\n").join(self.chart.plot_lines(self.size.width, self.size.height, self))

    def _bucket_at(self, x: int, y: int) -> Optional[int]:
        hit = self.hit
        if hit is None or not hit.top <= y < hit.bottom:
            return None
        col = x - hit.gutter
        if 0 <= col < hit.count * hit.slot:
            return hit.first + col // hit.slot
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        i = self._bucket_at(event.x, event.y)
        if i is not None:
            self.chart.highlight(i, mouse=True)

    def on_click(self, event: events.Click) -> None:
        self.on_mouse_move(event)

    def on_leave(self, event: events.Leave) -> None:
        self.chart.mouse_left()

    def _wheel(self, event: events.MouseEvent, factor: float) -> None:
        event.stop()
        i = self._bucket_at(event.x, event.y)
        if i is not None:
            self.chart.highlight(i, mouse=True)
        self.chart.zoom(factor)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, 0.5)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, 2.0)


class Legend(Widget):
    """`■ <label> <share>` per segment, wrapping. Hover or click picks an entry; click it again to let go."""

    def __init__(self, chart: "TimelineChart") -> None:
        super().__init__()
        self.chart = chart
        self.hits: List[Tuple[int, int, int, str]] = []  # (y, x0, x1, key)

    def _entries(self) -> List[Tuple[Group, str, str]]:
        d = self.chart.data
        return [(s, _fit(s.label, LEGEND_LABEL_MAX), fmt_pct(s.value(self.chart.metric), d.whole))
                for s in d.segs]

    def layout_lines(self, width: int, chart_height: int) -> Tuple[List[List[Tuple[str, int]]], int]:
        """The wrapped entries in at most the lines that leave the plot PLOT_MIN of the chart's rows, and how
        many entries did not fit."""
        if not self.chart.has_data():
            return [], 0
        metric = self.chart.metric
        entries = [(s.key, 2 + cell_len(label) + 1 + cell_len(share), s.value(metric))
                   for s, label, share in self._entries()]
        return fit_legend(entries, max(1, width), max(1, chart_height - PLOT_MIN))

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self.layout_lines(width, container.height)[0])

    def render(self) -> Text:
        chart = self.chart
        lines, hidden = self.layout_lines(self.size.width, chart.content_size.height)
        entries = {s.key: (s, label, share) for s, label, share in self._entries()}
        focus, only = chart.focus_key, chart.only
        self.hits = []
        out = []
        for y, line in enumerate(lines):
            text = Text()
            for key, x in line:
                seg, label, share = entries[key]
                text.append(" " * (x - text.cell_len))
                dim = (only is not None and key != only) or (only is None and focus is not None and key != focus)
                picked = key in (focus, only)
                text.append(SWATCH + " ", style=blend(seg.color, BG, DIM_MIX) if dim else seg.color)
                text.append(label, style=MUTED if dim else (f"bold {BRIGHT}" if picked else TEXT))
                text.append(" " + share, style=MUTED)
                self.hits.append((y, x, text.cell_len, key))
            out.append(text)
        if hidden and out:
            out[-1].append(" " * (LEGEND_GAP if out[-1].cell_len else 0) + "+%d" % hidden, style=MUTED)
        return Text("\n").join(out)

    def _key_at(self, x: int, y: int) -> Optional[str]:
        for hy, x0, x1, key in self.hits:
            if hy == y and x0 <= x < x1:
                return key
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.chart.hover(self._key_at(event.x, event.y))

    def on_leave(self, event: events.Leave) -> None:
        self.chart.hover(None)

    def on_click(self, event: events.Click) -> None:
        key = self._key_at(event.x, event.y)
        if key is not None:
            self.chart.pick(None if self.chart.legend_key == key else key)


class TimelineChart(Vertical):
    """The stacked timeline. `update` hands it the section's rows; `window` is the zoomed `(start_ms, end_ms)`
    or None, `only` the one segment key shown or None. Posts ViewChanged when the bucket size on screen or
    the zoom changes, so the sentence can name the bucket actually drawn."""

    DEFAULT_CSS = """
    TimelineChart { height: 1fr; min-height: 1; padding: 0 1; }
    TimelineChart > Plot { height: 1fr; min-height: 1; }
    TimelineChart > Legend { height: auto; }
    """

    window: reactive[Optional[Tuple[int, int]]] = reactive(None, init=False)
    only: reactive[Optional[str]] = reactive(None, init=False)

    class ViewChanged(Message):
        def __init__(self, g_ms: int, zoomed: bool) -> None:
            super().__init__()
            self.g_ms, self.zoomed = g_ms, zoomed

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: List[dict] = []
        self.slots: Optional[dict] = None
        self.by, self.metric = "model", "cost"
        self.picked_ms, self.range_from, self.range_to = 0, 0, 0
        self.empty = "无数据"
        self.data = ChartData([], 0.0, None, None, 0)
        self.hl_ms: Optional[int] = None  # a time in the highlighted bucket; None: no bar is highlighted
        self._hl_mouse = False  # the highlight follows the mouse, so it goes when the mouse leaves
        self.legend_key: Optional[str] = None  # picked with ↑/↓ or a click
        self.hover_key: Optional[str] = None  # the legend entry under the mouse
        self._view: Optional[Tuple[int, bool]] = None
        self._peak_for: Optional[tuple] = None
        self._peak_value = 0.0
        self.plot = Plot(self)
        self.legend = Legend(self)

    def compose(self) -> ComposeResult:
        yield self.plot
        yield self.legend

    # ------------------------------------------------------------ state

    def update(self, rows: List[dict], by: str, metric: str, picked_ms: int, range_from: int, range_to: int,
               empty: str = "无数据", slots: Optional[dict] = None) -> None:
        """`rows` are the section's (device and range applied); `slots` is model_slots() over every row, so
        the colours match the share table's; `empty` is what to say when there is nothing to draw."""
        self.rows, self.by, self.metric, self.slots = rows, by, metric, slots
        self.picked_ms, self.range_from, self.range_to = picked_ms, range_from, range_to
        self.empty = empty
        self._rebuild()

    def reset_view(self, keep_only: bool = False) -> None:
        """Back to the whole axis with no bar highlighted; also forget the legend entry and `only` unless
        `keep_only`. The next update() redraws."""
        self.hl_ms = None
        self.set_reactive(TimelineChart.window, None)
        if not keep_only:
            self.legend_key = self.hover_key = None
            self.set_reactive(TimelineChart.only, None)

    def watch_window(self) -> None:
        self._rebuild()

    def watch_only(self) -> None:
        self._redraw()

    def _rebuild(self) -> None:
        self.data = build_chart(self.rows, self.by, self.metric, self.picked_ms or MINUTE_MS, self.range_to,
                                self.slots, self.window)
        keys = {s.key for s in self.data.segs}
        if self.window is not None and self.data.window is None:
            self.set_reactive(TimelineChart.window, None)
        if self.only is not None and self.only not in keys:
            self.set_reactive(TimelineChart.only, None)
        if self.legend_key not in keys:
            self.legend_key = None
        if self.hover_key not in keys:
            self.hover_key = None
        view = (self.data.g, self.data.window is not None)
        if view != self._view:
            self._view = view
            self.post_message(self.ViewChanged(*view))
        self._redraw()

    def _redraw(self) -> None:
        self.plot.refresh()
        self.legend.refresh(layout=True)

    def has_data(self) -> bool:
        return bool(self.data.starts) and self.data.whole > 0

    @property
    def focus_key(self) -> Optional[str]:
        return self.hover_key or self.legend_key

    def hl_index(self) -> int:
        """The highlighted bucket, or the latest one when none is (what zoom anchors on)."""
        n = len(self.data.starts)
        if self.hl_ms is None:
            return n - 1
        return min(max(bisect.bisect_right(self.data.starts, self.hl_ms) - 1, 0), n - 1)

    # ------------------------------------------------------------ actions (the app's keys call these)

    def highlight(self, i: int, mouse: bool = False) -> None:
        starts = self.data.starts
        if not starts:
            return
        t = starts[min(max(i, 0), len(starts) - 1)]
        self._hl_mouse = mouse
        if t != self.hl_ms:
            self.hl_ms = t
            self.plot.refresh()

    def mouse_left(self) -> None:
        if self._hl_mouse and self.hl_ms is not None:
            self.hl_ms = None
            self.plot.refresh()

    def move(self, step: int) -> None:
        """Move the highlight; the first press highlights the latest bar."""
        if self.data.starts:
            self.highlight(self.hl_index() + (step if self.hl_ms is not None else 0))

    def zoom(self, factor: float) -> None:
        d = self.data
        if d.axis is None or not d.starts:
            return
        i = self.hl_index()
        anchor = (d.starts[i] + d.ends[i]) // 2
        if self.hl_ms is not None:
            self.hl_ms = anchor
        self.window = zoom_window(d.window, d.axis, anchor, factor)
        self.plot.refresh()

    def pan_or_move(self, direction: int) -> None:
        d = self.data
        if d.window is None:
            self.move(direction)
            return
        new = pan_window(d.window, d.axis, direction)
        if self.hl_ms is not None:
            self.hl_ms = min(max(self.hl_ms, new[0]), new[1] - 1)
        self.window = new

    def step_legend(self, step: int) -> None:
        keys = [s.key for s in self.data.segs]
        if not keys:
            return
        at = keys.index(self.legend_key) if self.legend_key in keys else (-1 if step > 0 else len(keys))
        at += step
        self.pick(keys[at] if 0 <= at < len(keys) else None)

    def pick(self, key: Optional[str]) -> None:
        self.legend_key = key
        self._redraw()

    def clear(self) -> None:
        """Esc: no highlighted bar, no picked legend entry."""
        self.hl_ms = None
        self.pick(None)

    def hover(self, key: Optional[str]) -> None:
        if key != self.hover_key:
            self.hover_key = key
            self._redraw()

    def isolate(self) -> None:
        """Show only the picked segment; for the one already shown alone, or with nothing picked, show all."""
        key = self.focus_key
        self.only = None if key is None or key == self.only else key

    # ------------------------------------------------------------ drawing

    def _peak(self, drawn: List[Group]) -> float:
        """The tallest stack of the view; kept until the data or `only` changes, as hovering redraws a lot."""
        key = (self.data, self.only)
        if self._peak_for is None or self._peak_for[0] is not self.data or self._peak_for[1] != self.only:
            keys = [s.key for s in drawn]
            self._peak_value = max([sum(v.get(k, 0.0) for k in keys) for v in self.data.values.values()] or [0.0])
            self._peak_for = key
        return self._peak_value

    def _colors(self) -> Dict[str, str]:
        focus = None if self.only is not None else self.focus_key
        return {s.key: s.color if focus in (None, s.key) else blend(s.color, BG, DIM_MIX) for s in self.data.segs}

    def plot_lines(self, width: int, height: int, plot: Plot) -> List[Text]:
        plot.hit = None
        if width <= 0 or height <= 0:
            return []
        if not self.has_data():
            lines = [Text("") for _ in range(height)]
            message = self.empty if not self.rows else "无数据"
            lines[height // 2] = Text(" " * max(0, (width - cell_len(message)) // 2) + message, style=MUTED)
            return lines
        d = self.data
        drawn = [s for s in d.segs if self.only is None or s.key == self.only]
        colors = self._colors()
        hl = self.hl_index()
        top_rows = 1 if height >= 4 else 0
        label_rows = 1 if height >= 3 else 0
        axis_rows = 1 if height >= 2 else 0
        bars_h = max(1, height - top_rows - label_rows - axis_rows)

        vmax = nice_max(self._peak(drawn))
        y_label = fmt_metric(vmax, self.metric)
        gutter = cell_len(y_label) + 1
        first, count, slot, bar = bar_geometry(len(d.starts), max(1, width - gutter), hl)
        bars_w = count * slot  # the axis ends with the last bar
        offset = slot - bar
        stacks = [[(d.values.get(i, {}).get(s.key, 0.0), colors[s.key]) for s in drawn]
                  for i in range(first, first + count)]
        cells = render_bars(stacks, bars_h, vmax)
        lit = self.hl_ms is not None and first <= hl < first + count
        hl_cols = range((hl - first) * slot + offset, (hl - first + 1) * slot) if lit else range(0)

        lines: List[Text] = []
        if top_rows:
            lines.append(self._top_line(width, gutter, bars_w))
        for r in range(bars_h):
            if r == 0:
                line = Text(_pad(y_label, gutter - 1, right=True) + "┤", style=MUTED)
            else:
                line = Text(" " * (gutter - 1) + "│", style=RULE)
            for c in range(bars_w):
                k, within = divmod(c, slot)
                band = BAND if c in hl_cols else None
                if within < offset:
                    line.append(" ", style=_style(None, band))
                else:
                    glyph, fg, bg = cells[r][k]
                    line.append(glyph, style=_style(fg, bg or band))
            lines.append(line)
        if axis_rows:
            line = Text(" " * (gutter - 1) + "└", style=RULE)
            for c in range(bars_w):
                line.append("─", style=HL_AXIS if c in hl_cols else RULE)
            lines.append(line)
        if label_rows:
            line = Text(" " * gutter)
            end = d.ends[first + count - 1]
            for col, text in x_labels(d.starts, end, first, count, slot, bar, d.g, bars_w):
                line.append(" " * (gutter + col - line.cell_len))
                line.append(text, style=BRIGHT if col in hl_cols else MUTED)
            lines.append(line)
        if lit:
            bar_x = gutter + (hl - first) * slot + offset
            self._overlay_detail(lines, top_rows, width, bar_x, gutter + (hl - first + 1) * slot)
        plot.hit = Hit(top_rows, top_rows + bars_h + axis_rows + label_rows, gutter, first, count, slot)
        return lines

    def _overlay_detail(self, lines: List[Text], top: int, width: int, bar_x0: int, bar_x1: int) -> None:
        """Draw the highlighted bucket's breakdown in a box over the plot, right of the bar if it fits there,
        else left of it. The rows it covers hold one-cell glyphs only, so characters and cells line up."""
        room = len(lines) - top
        border = room >= 5 and width >= 12  # a border only when it leaves three rows inside
        inner = max(1, min(self._detail_width(), width - (4 if border else 2)))
        body = self.detail_lines(room - 2 if border else room, inner)
        if not body:
            return
        box_w = min(width, inner + (4 if border else 2))
        x = bar_x1 + 1
        if x + box_w > width:
            x = bar_x0 - 1 - box_w
        if x < 0:
            x = max(0, width - box_w)
        edge = f"{BOX_EDGE} on {SURFACE}"
        rows = []
        if border:
            rows.append(Text("╭" + "─" * (box_w - 2) + "╮", style=edge))
        for text in body:
            row = Text(style=f"on {SURFACE}")
            row.append("│ " if border else " ", style=edge)
            row.append_text(text)
            row.append(" " * (box_w - row.cell_len - (2 if border else 1)))
            row.append(" │" if border else " ", style=edge)
            rows.append(row)
        if border:
            rows.append(Text("╰" + "─" * (box_w - 2) + "╯", style=edge))
        for j, row in enumerate(rows):
            line = lines[top + j]
            if line.cell_len < x + box_w:
                line.append(" " * (x + box_w - line.cell_len))
            lines[top + j] = Text.assemble(line[:x], row, line[x + box_w:])

    def _top_line(self, width: int, gutter: int, bars_w: int) -> Text:
        """The range's total (or the shown segment's) centred over the bars; on its left the picked or shown
        segment's value and share, or the zoomed window; the key hints on the right."""
        d = self.data
        segs = {s.key: s for s in d.segs}
        only = segs.get(self.only) if self.only else None
        total_text = fmt_metric(only.value(self.metric) if only else d.whole, self.metric)
        start = max(0, gutter + (bars_w - cell_len(total_text)) // 2)
        left = Text()
        shown = only or segs.get(self.focus_key)
        if shown is not None:
            if only is not None:
                left.append("只看 ", style=MUTED)
            left.append(SWATCH + " ", style=shown.color)
            left.append(shown.label + " ", style=BRIGHT)
            v = shown.value(self.metric)
            left.append(fmt_metric(v, self.metric), style=BRIGHT)
            left.append(" · " + fmt_pct(v, d.whole), style=MUTED)
        elif d.window is not None:
            left.append(self._window_text(d.window), style=MUTED)
        line = Text()
        if left.cell_len and start - 2 > 0:
            if left.cell_len > start - 2:
                left.truncate(start - 3)
                left.append("~", style=MUTED)
            line.append_text(left)
        line.append(" " * (start - line.cell_len))
        line.append(total_text, style=f"bold {BRIGHT}")
        for keep in range(len(HINTS), 0, -1):
            hints = Text()
            for i, (key, word) in enumerate(HINTS[:keep]):
                hints.append(("  " if i else "") + key, style=TEXT)
                hints.append(" " + word, style=MUTED)
            if line.cell_len + 2 + hints.cell_len <= width:
                line.append(" " * (width - hints.cell_len - line.cell_len))
                line.append_text(hints)
                break
        return line

    @staticmethod
    def _window_text(window: Tuple[int, int]) -> str:
        a, last, b = (time.localtime(t / 1000) for t in (window[0], window[1] - 1, window[1]))
        same_day = (a.tm_year, a.tm_yday) == (last.tm_year, last.tm_yday)
        end = "24:00" if same_day and _midnight(window[1]) == window[1] else \
            time.strftime("%H:%M" if same_day else "%m/%d %H:%M", b)
        return "%s – %s" % (time.strftime("%m/%d %H:%M", a), end)

    def _name_width(self) -> int:
        widest = max([cell_len(s.label) for s in self.data.segs] + [cell_len("合计")])
        return min(NAME_MAX, max(NAME_MIN, widest))

    def _detail_width(self) -> int:
        """The breakdown's width: the same for every bucket of the view, so the box does not jump about."""
        d = self.data
        title = bucket_title(d.starts[0], d.ends[0], d.g) if d.starts else ""
        return max(2 + self._name_width() + 1 + VALUE_W + 1 + PCT_W, cell_len(title) + 1)

    def detail_lines(self, room: int, width: int) -> List[Text]:
        """The highlighted bucket: its time, then `■ name value share` per segment, largest first, then 合计
        (no share), each `width` cells. Rows that do not fit fold into `另 N 段`; with one line of room it is
        `time · 合计 value`."""
        d = self.data
        if room <= 0 or not d.starts:
            return []
        i = self.hl_index()
        title = bucket_title(d.starts[i], d.ends[i], d.g)
        bucket_total = d.totals.get(i, 0.0)
        total_text = fmt_metric(bucket_total, self.metric)
        if room == 1:
            return [Text(_fit(title + " · 合计 " + total_text, width), style=BRIGHT)]
        name_w = max(1, min(self._name_width(), width - (2 + 1 + VALUE_W + 1 + PCT_W)))
        items = bucket_detail(d.values.get(i, {}), d.segs)
        rest: List[Tuple[Group, float]] = []
        body = room - 2
        if len(items) > body:  # with two rows or more, the last one says what the others add up to
            keep = body - 1 if body >= 2 else body
            items, rest = items[:keep], (items[keep:] if body >= 2 else [])
        focus = self.only or self.focus_key
        lines = [Text(_fit(title, width), style=BRIGHT)]

        def numbers(line: Text, v: float, dim: bool) -> None:
            line.append(" " + _pad(fmt_metric(v, self.metric), VALUE_W, right=True), style=MUTED if dim else BRIGHT)
            line.append(" " + _pad(fmt_pct(v, bucket_total), PCT_W, right=True), style=MUTED)

        for seg, v in items:
            dim = focus is not None and seg.key != focus
            line = Text()
            line.append(SWATCH + " ", style=blend(seg.color, BG, DIM_MIX) if dim else seg.color)
            line.append(_pad(_fit(seg.label, name_w), name_w), style=MUTED if dim else TEXT)
            numbers(line, v, dim)
            lines.append(line)
        if rest:
            line = Text("  " + _pad(_fit("另 %d 段" % len(rest), name_w), name_w), style=MUTED)
            numbers(line, sum(v for _, v in rest), True)
            lines.append(line)
        line = Text("  " + _pad("合计", name_w), style=TEXT)
        line.append(" " + _pad(total_text, VALUE_W, right=True), style=f"bold {BRIGHT}")
        lines.append(line)
        return lines
