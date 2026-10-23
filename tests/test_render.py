"""Pure timeline helpers: bar cells, buckets per segment, geometry, axis labels, zoom, the bucket breakdown."""
import time

import pytest

pytest.importorskip("textual")

from tui_timeline import (BLANK, bar_geometry, bucket_detail, bucket_starts, bucket_title, build_chart,  # noqa: E402
                          fit_legend, legend_lines, nice_max, pan_window, render_bars, x_labels, zoom_window)
from usage_groups import OTHER_KEY, segments  # noqa: E402
from usage_time import DAY_MS, MIN_SPAN_MS, MINUTE_MS, bucket_start, zoom_granularity  # noqa: E402

H = 60 * MINUTE_MS


def local_ms(y, mo, d, h=0, mi=0):
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))) * 1000


def row(t, project="p", cost=1.0, model="claude-opus-5-5", effort="high", tokens=1000):
    return {"t": t, "model": model, "effort": effort, "project": project, "skill": "none", "device": "a",
            "input": tokens, "output": 0, "cache_write": 0, "cache_write_1h": 0, "cache_read": 0, "cost": cost,
            "premium": False}


# ---------------------------------------------------------------- render_bars

def test_render_bars_eighths():
    # one column, height 2 rows = 16 eighths, vmax 16: red 3/16, blue 6/16
    cells = render_bars([[(3, "R"), (6, "B")]], height=2, vmax=16)
    bottom, top = cells[1][0], cells[0][0]
    # bottom cell: red 3/8 under blue -> ("▃", "R", "B"); top cell: blue 1/8 -> ("▁", "B", None)
    assert bottom == ("▃", "R", "B") and top == ("▁", "B", None)


def test_render_bars_empty_and_zero():
    assert render_bars([], height=3, vmax=10) == [[], [], []]
    assert render_bars([[(5, "R")]], height=2, vmax=0) == [[BLANK], [BLANK]]
    assert render_bars([[(0, "R")], []], height=2, vmax=10) == [[BLANK, BLANK], [BLANK, BLANK]]
    assert render_bars([[(5, "R")]], height=0, vmax=10) == []


def test_render_bars_full_cells_top_first_and_one_column_per_stack():
    cells = render_bars([[(16, "R")], [(12, "G")], [(4, "B")]], height=2, vmax=16)
    assert len(cells) == 2 and all(len(r) == 3 for r in cells)
    assert [cells[0][0], cells[1][0]] == [("█", "R", None)] * 2
    assert cells[1][1] == ("█", "G", None) and cells[0][1] == ("▄", "G", None)
    assert cells[1][2] == ("▄", "B", None) and cells[0][2] == BLANK


def test_render_bars_clips_above_vmax():
    assert render_bars([[(30, "R")]], height=1, vmax=8) == [[("█", "R", None)]]


def test_render_bars_more_than_one_boundary_goes_to_the_biggest_segment():
    # thirds of one cell round to 3, 5, 8 eighths: A3 B2 C3, A and C tie, the lower one (A) takes B's eighths
    assert render_bars([[(1, "A"), (1, "B"), (1, "C")]], height=1, vmax=3) == [[("▅", "A", "C")]]
    # A1 B6 C1: the middle one holds the most; the top keeps its eighth, B takes A's
    assert render_bars([[(1, "A"), (6, "B"), (1, "C")]], height=1, vmax=8) == [[("▇", "B", "C")]]
    # A2 B5 C1: the bottom piece is bigger than the top one, so it stays and B takes C's
    assert render_bars([[(2, "A"), (5, "B"), (1, "C")]], height=1, vmax=8) == [[("▂", "A", "B")]]
    # the stack ends inside the cell: its height is kept, the segment with the most of the cell fills it
    assert render_bars([[(2, "A"), (3, "B")]], height=1, vmax=8) == [[("▅", "B", None)]]


def test_render_bars_heights_follow_the_rounded_running_total():
    stacks = [[(v, "A"), (v, "B")] for v in (0.4, 1.3, 2.6, 3.9)]
    cells = render_bars(stacks, height=1, vmax=8)
    heights = [" ▁▂▃▄▅▆▇█".index(c[0]) if c[2] is None else 8 for c in cells[0]]
    assert heights == [1, 3, 5, 8]  # round(0.8), round(2.6), round(5.2), round(7.8)


def test_render_bars_same_colour_neighbours_count_as_one():
    assert render_bars([[(2, "G"), (3, "G"), (3, "R")]], height=1, vmax=8) == [[("▅", "G", "R")]]


def test_nice_max():
    assert nice_max(0) == 0
    assert nice_max(37) == 40 and nice_max(40) == 40 and nice_max(7) == 8 and nice_max(101) == 120
    assert nice_max(0.27) == pytest.approx(0.3) and nice_max(3) == 3


# ---------------------------------------------------------------- buckets

def test_bucket_starts_plain_day():
    day = local_ms(2026, 10, 16)
    starts = bucket_starts(day + H, day + DAY_MS, 4 * H)
    assert starts == [day + k * 4 * H for k in range(6)]
    assert bucket_starts(day, day + 3 * DAY_MS - 1, DAY_MS) == [bucket_start(day + k * DAY_MS + 2 * H, DAY_MS)
                                                                 for k in range(3)]


def test_bucket_starts_follow_local_midnight_on_a_long_day(tz_new_york):
    day = local_ms(2026, 11, 1)  # 25 hours in New York
    nxt = local_ms(2026, 11, 2)
    starts = bucket_starts(day, nxt + 1, 4 * H)
    assert starts[:7] == [day + k * 4 * H for k in range(7)]  # the 7th starts 24 h in and is cut short
    assert starts[7] == nxt and len(starts) == 8


def test_build_chart_buckets_rows_per_segment_with_other_and_specks():
    t0 = local_ms(2026, 10, 16, 9)
    rows = [row(t0 + k * MINUTE_MS, project="p%d" % k, cost=10.0) for k in range(6)]
    rows += [row(t0 + H, project="p6", cost=5.0), row(t0 + H, project="p7", cost=4.0)]
    data = build_chart(rows, "project", "cost", H, local_ms(2026, 10, 16, 12, 30), None, None)
    assert data.axis == (t0, local_ms(2026, 10, 16, 13))  # first used bucket to the end of the range's bucket
    assert data.g == H and data.starts == [t0, t0 + H, t0 + 2 * H, t0 + 3 * H]
    assert data.values[0] == {"p%d" % k: 10.0 for k in range(6)}
    assert data.values[1] == {OTHER_KEY: 9.0} and data.totals[1] == 9.0
    assert 2 not in data.values and data.whole == 69.0

    big = [row(t0, model="claude-opus-5-5", cost=1000.0), row(t0, model="claude-sonnet-5-5", cost=0.5)]
    data = build_chart(big, "model", "cost", H, t0 + H, None, None)
    assert [s.key for s in data.segs] == ["claude-opus-5-5\thigh"]  # the 0.05% speck is not a segment
    assert data.values[0] == {"claude-opus-5-5\thigh": 1000.0} and data.totals[0] == 1000.5


def test_build_chart_window_rebuckets_finer_and_uses_the_given_slots():
    t0 = local_ms(2026, 10, 16)
    rows = [row(t0 + k * H, cost=1.0) for k in range(24)]
    full = build_chart(rows, "model", "requests", 4 * H, t0 + DAY_MS, None, None)
    assert full.axis == (t0, t0 + DAY_MS) and len(full.starts) == 6
    window = (t0 + 6 * H, t0 + 10 * H)
    zoomed = build_chart(rows, "model", "requests", 4 * H, t0 + DAY_MS, None, window)
    assert zoomed.window == window
    assert zoomed.g == zoom_granularity(4 * H, DAY_MS, 4 * H) < 4 * H
    assert zoomed.starts[0] == window[0] and zoomed.ends[-1] == window[1]
    assert sum(zoomed.totals.values()) == 4
    # a window as wide as the axis is no zoom at all
    assert build_chart(rows, "model", "requests", 4 * H, t0 + DAY_MS, None, (t0 - H, t0 + 2 * DAY_MS)).window is None
    slots = {"claude-opus-5-5": 3}
    assert build_chart(rows, "model", "cost", 4 * H, t0 + DAY_MS, slots, None).segs[0].color == "#b57808"


def test_build_chart_without_rows():
    data = build_chart([], "model", "cost", H, 0, None, None)
    assert data.axis is None and data.starts == [] and data.whole == 0


# ---------------------------------------------------------------- geometry and labels

def test_bar_geometry():
    assert bar_geometry(10, 60, 9) == (0, 10, 6, 5)  # 6 columns per bucket: a 5-wide bar and a gap
    assert bar_geometry(20, 60, 19) == (0, 20, 3, 2)
    assert bar_geometry(30, 60, 29) == (0, 30, 2, 2)  # a gap would leave one column: no gap
    assert bar_geometry(100, 60, 99) == (40, 60, 1, 1)  # the latest buckets show
    assert bar_geometry(100, 60, 10) == (10, 60, 1, 1)  # until the highlight goes further back
    assert bar_geometry(0, 60, 0) == (0, 0, 0, 0)


def test_x_labels_dates_for_a_week_and_times_for_a_day():
    day = local_ms(2026, 10, 16)
    starts = bucket_starts(day, day + 7 * DAY_MS, 4 * H)
    labels = x_labels(starts, starts[-1] + 4 * H, 0, 42, 2, 2, 4 * H, 84)
    assert [text for _, text in labels] == ["10/%d" % d for d in range(16, 23)]
    assert [col for col, _ in labels] == [12 * k for k in range(7)]

    starts = bucket_starts(day, day + DAY_MS, 30 * MINUTE_MS)
    labels = x_labels(starts, day + DAY_MS, 0, 48, 2, 2, 30 * MINUTE_MS, 96)
    assert labels[0] == (0, "10/16") and labels[1] == (8, "02:00")
    assert all(b[0] >= a[0] + len(a[1]) + 2 for a, b in zip(labels, labels[1:]))
    assert all(col + len(text) <= 96 for col, text in labels)


def test_x_labels_leave_room_between_labels_when_bars_are_thin():
    day = local_ms(2026, 10, 1)
    starts = bucket_starts(day, day + 30 * DAY_MS, DAY_MS)
    labels = x_labels(starts, day + 30 * DAY_MS, 0, 30, 1, 1, DAY_MS, 30)
    assert len(labels) >= 2
    assert all(b[0] >= a[0] + len(a[1]) + 2 for a, b in zip(labels, labels[1:]))


# ---------------------------------------------------------------- zoom and pan

def test_zoom_window_keeps_the_anchor_in_place():
    axis = (0, 8 * H)
    assert zoom_window(None, axis, 2 * H, 0.5) == (H, 5 * H)
    assert zoom_window((H, 5 * H), axis, 2 * H, 0.5) == (int(1.5 * H), int(3.5 * H))
    assert zoom_window((0, 40 * MINUTE_MS), axis, 0, 0.5) == (0, MIN_SPAN_MS)  # never narrower than 30 minutes
    assert zoom_window((H, 5 * H), axis, 2 * H, 2) is None  # as wide as the axis: back to the whole axis
    assert zoom_window(None, (0, 20 * MINUTE_MS), 0, 0.5) is None  # nothing to zoom into
    assert zoom_window(None, axis, 8 * H - 1, 0.5) == (4 * H, 8 * H)  # clamped to the axis


def test_pan_window_moves_a_quarter_and_stops_at_the_ends():
    axis = (0, 8 * H)
    assert pan_window((H, 5 * H), axis, 1) == (2 * H, 6 * H)
    assert pan_window((4 * H, 8 * H), axis, 1) == (4 * H, 8 * H)
    assert pan_window((H // 2, 4 * H + H // 2), axis, -1) == (0, 4 * H)


# ---------------------------------------------------------------- the bucket's breakdown and the legend

def test_bucket_detail_is_largest_first_without_zeros():
    rows = [row(0, project="a", cost=3.0), row(0, project="b", cost=5.0), row(0, project="c", cost=1.0)]
    segs = segments(rows, "project", "cost")
    assert [(s.key, v) for s, v in bucket_detail({"a": 3.0, "b": 5.0, "c": 0.0}, segs)] == [("b", 5.0), ("a", 3.0)]


def test_bucket_title():
    s = local_ms(2026, 10, 16, 8)
    assert bucket_title(s, s + 4 * H, 4 * H) == "10/16 08:00–12:00"
    assert bucket_title(local_ms(2026, 10, 16, 20), local_ms(2026, 10, 17), 4 * H) == "10/16 20:00–24:00"
    assert bucket_title(local_ms(2026, 10, 16), local_ms(2026, 10, 17), DAY_MS) == "10/16"
    assert bucket_title(s + 3 * MINUTE_MS, s + 4 * MINUTE_MS, MINUTE_MS) == "10/16 08:03"


def test_legend_lines_wrap():
    assert legend_lines([("a", 10), ("b", 10), ("c", 10)], 25) == [[("a", 0), ("b", 13)], [("c", 0)]]
    assert legend_lines([("a", 30)], 20) == [[("a", 0)]]  # an entry wider than the line still gets one
    assert legend_lines([], 20) == []


def test_fit_legend_keeps_the_biggest_entries_in_order_with_room_for_the_count():
    entries = [("a", 10, 1.0), ("b", 10, 80.0), ("c", 10, 5.0), ("d", 10, 14.0)]
    assert fit_legend(entries, 60, 1) == ([[("a", 0), ("b", 13), ("c", 26), ("d", 39)]], 0)
    lines, hidden = fit_legend(entries, 30, 1)  # two fit beside a "+2"
    assert lines == [[("b", 0), ("d", 13)]] and hidden == 2
    lines, hidden = fit_legend(entries + [("e", 10, 2.0)], 25, 2)  # two lines of two: three and a "+2"
    assert [k for line in lines for k, _ in line] == ["b", "c", "d"] and hidden == 2
