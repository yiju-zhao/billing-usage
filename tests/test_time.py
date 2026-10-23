import calendar
import time

import pytest

from usage_time import (GRANULARITIES, MIN_SPAN_MS, RANGE_BUCKETS, RANGES, bucket_start, cycle_start_ms,
                        range_bounds, zoom_granularity)

MIN = 60_000
HOUR = 3600_000
D = 86400_000


def ms(iso):
    """Epoch milliseconds of a 'YYYY-MM-DDTHH:MM:SSZ' string, computed without the code under test."""
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) * 1000


def bounds(key, now_ms, cycle_day=8, week_resets_at_ms=None, custom_start="", custom_end=""):
    return range_bounds(key, now_ms, cycle_day=cycle_day, week_resets_at_ms=week_resets_at_ms,
                        custom_start=custom_start, custom_end=custom_end)


def test_tables():
    assert [key for key, _ in RANGES] == ["today", "cycle", "week", "7d", "30d", "all", "custom"]
    assert dict(RANGES)["week"] == "自每周重置以来"
    assert GRANULARITIES == [("1m", "1 分钟", MIN), ("5m", "5 分钟", 5 * MIN), ("15m", "15 分钟", 15 * MIN),
                             ("30m", "30 分钟", 30 * MIN), ("1h", "1 小时", HOUR), ("4h", "4 小时", 4 * HOUR),
                             ("1d", "1 天", D)]
    assert RANGE_BUCKETS == {"today": "30m", "week": "4h", "7d": "4h", "30d": "1d", "all": "1d", "cycle": "1d"}
    assert set(RANGE_BUCKETS.values()) <= {key for key, _, _ in GRANULARITIES}
    assert MIN_SPAN_MS == 30 * MIN


def test_cycle_start():
    assert cycle_start_ms(ms("2026-10-22T05:00:00Z"), 8) == ms("2026-10-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-10-08T00:00:00Z"), 8) == ms("2026-10-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-10-07T23:59:59Z"), 8) == ms("2026-09-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-01-03T00:00:00Z"), 8) == ms("2025-12-08T00:00:00Z")


def test_cycle_start_edge_days_and_milliseconds():
    assert cycle_start_ms(ms("2026-03-01T00:00:00Z"), 1) == ms("2026-03-01T00:00:00Z")
    assert cycle_start_ms(ms("2026-03-01T00:00:00Z") - 1, 1) == ms("2026-02-01T00:00:00Z")
    assert cycle_start_ms(ms("2026-03-27T23:59:59Z") + 999, 28) == ms("2026-02-28T00:00:00Z")
    assert cycle_start_ms(ms("2026-03-28T00:00:00Z"), 28) == ms("2026-03-28T00:00:00Z")


def test_cycle_start_ignores_local_timezone(tz_new_york):
    # 2026-10-08 00:00 UTC is still Oct 7 in New York; the cycle boundary stays on UTC midnight.
    assert cycle_start_ms(ms("2026-10-08T00:30:00Z"), 8) == ms("2026-10-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-10-08T03:59:00Z"), 8) == ms("2026-10-08T00:00:00Z")


def test_week_range():
    now = ms("2026-10-22T05:00:00Z")
    resets = ms("2026-10-24T10:00:00Z")
    assert bounds("week", now, week_resets_at_ms=resets) == (ms("2026-10-17T10:00:00Z"), now)
    assert bounds("week", now, week_resets_at_ms=None) is None


def test_week_range_steps_back_when_reset_is_over_seven_days_away():
    now = ms("2026-10-22T05:00:00Z")
    resets = ms("2026-10-30T10:00:00Z")          # 8 days ahead: resets - 7 d is still in the future
    assert bounds("week", now, week_resets_at_ms=resets) == (ms("2026-10-16T10:00:00Z"), now)
    resets = ms("2026-11-13T10:00:00Z")          # three weeks ahead
    assert bounds("week", now, week_resets_at_ms=resets) == (ms("2026-10-16T10:00:00Z"), now)
    exactly_now = bounds("week", now, week_resets_at_ms=now + 7 * D)
    assert exactly_now == (now, now)


def test_simple_ranges():
    now = ms("2026-10-22T05:00:00Z")
    assert bounds("7d", now) == (now - 7 * D, now)
    assert bounds("30d", now) == (now - 30 * D, now)
    assert bounds("all", now) == (0, now)
    assert bounds("cycle", now, cycle_day=8) == (ms("2026-10-08T00:00:00Z"), now)
    assert bounds("cycle", now, cycle_day=25) == (ms("2026-09-25T00:00:00Z"), now)


def test_unknown_range_is_an_error():
    with pytest.raises(ValueError):
        bounds("fortnight", ms("2026-10-22T05:00:00Z"))


def test_today_and_custom_local(tz_new_york):
    now = ms("2026-10-22T07:10:00Z")             # 03:10 EDT
    midnight = ms("2026-10-22T04:00:00Z")        # 00:00 EDT
    assert bounds("today", now) == (midnight, now)
    # Just after UTC midnight it is still the previous evening in New York.
    late = ms("2026-10-22T02:00:00Z")            # 22:00 EDT on the 21st
    assert bounds("today", late) == (ms("2026-10-21T04:00:00Z"), late)

    # A custom range is whole local days; the end day is included.
    assert bounds("custom", now, custom_start="2026-10-20", custom_end="2026-10-21") == \
        (ms("2026-10-20T04:00:00Z"), ms("2026-10-22T04:00:00Z"))
    # An end day that reaches past now is capped at now.
    assert bounds("custom", now, custom_start="2026-10-20", custom_end="2026-10-22") == \
        (ms("2026-10-20T04:00:00Z"), now)
    assert bounds("custom", now, custom_start="2026-10-20", custom_end="2026-12-31") == \
        (ms("2026-10-20T04:00:00Z"), now)
    # Empty end means now; empty start means local midnight today.
    assert bounds("custom", now, custom_start="2026-10-20", custom_end="") == (ms("2026-10-20T04:00:00Z"), now)
    assert bounds("custom", now, custom_start="", custom_end="") == (midnight, now)
    assert bounds("custom", now, custom_start="", custom_end="2026-10-22") == (midnight, now)


def test_custom_across_the_autumn_clock_change(tz_new_york):
    # 2026-11-01 is 25 hours long in New York: midnight is 04:00Z (EDT), the next midnight is 05:00Z (EST).
    now = ms("2026-11-20T12:00:00Z")
    assert bounds("custom", now, custom_start="2026-10-31", custom_end="2026-11-01") == \
        (ms("2026-10-31T04:00:00Z"), ms("2026-11-02T05:00:00Z"))
    assert bounds("custom", now, custom_start="2026-11-01", custom_end="2026-11-01") == \
        (ms("2026-11-01T04:00:00Z"), ms("2026-11-02T05:00:00Z"))


def test_bad_custom_date_is_an_error():
    with pytest.raises(ValueError):
        bounds("custom", ms("2026-10-22T05:00:00Z"), custom_start="22/10/2026")


def test_bucket_start_local(tz_new_york):
    t = ms("2026-10-22T07:10:00Z")               # 03:10 EDT
    assert bucket_start(t, 4 * 3600_000) == ms("2026-10-22T04:00:00Z")   # 00:00 EDT
    assert bucket_start(t, 30 * 60_000) == ms("2026-10-22T07:00:00Z")
    assert bucket_start(t, 60_000) == ms("2026-10-22T07:10:00Z")
    assert bucket_start(t, D) == ms("2026-10-22T04:00:00Z")


def test_bucket_start_is_exact_on_boundaries_and_keeps_milliseconds_out(tz_new_york):
    midnight = ms("2026-10-22T04:00:00Z")
    assert bucket_start(midnight, D) == midnight
    assert bucket_start(midnight - 1, D) == ms("2026-10-21T04:00:00Z")
    assert bucket_start(midnight + 59_999, 60_000) == midnight
    assert bucket_start(midnight + 60_000, 60_000) == midnight + 60_000


def test_bucket_start_utc_equivalent_when_timezone_is_utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        t = ms("2026-10-22T07:10:00Z")
        assert bucket_start(t, 4 * HOUR) == ms("2026-10-22T04:00:00Z")
        assert bucket_start(t, D) == ms("2026-10-22T00:00:00Z")
    finally:
        monkeypatch.undo()
        time.tzset()


def test_bucket_start_on_clock_change_days(tz_new_york):
    # Spring forward 2026-03-08: a 23-hour local day from 05:00Z to 04:00Z the next day.
    midnight = ms("2026-03-08T05:00:00Z")
    next_midnight = ms("2026-03-09T04:00:00Z")
    # Autumn back 2026-11-01: a 25-hour local day from 04:00Z to 05:00Z the next day.
    midnight_fall = ms("2026-11-01T04:00:00Z")
    next_midnight_fall = ms("2026-11-02T05:00:00Z")
    for first, after in ((midnight, next_midnight), (midnight_fall, next_midnight_fall)):
        previous = None
        for t in range(first, after, 5 * 60_000):
            assert bucket_start(t, D) == first               # one day bucket for the whole local day
            for g in (4 * HOUR, HOUR, 30 * 60_000):
                start = bucket_start(t, g)
                assert first <= start <= t < start + g
            fine = bucket_start(t, 4 * HOUR)
            assert previous is None or fine >= previous      # bucket keys never go backwards in time
            previous = fine
        assert bucket_start(after, D) == after               # the next local midnight starts a new bucket


def test_zoom_granularity():
    assert zoom_granularity(4*3600_000, 7*D, 7*D) == 4*3600_000          # unzoomed -> picked
    assert zoom_granularity(4*3600_000, 7*D, MIN_SPAN_MS) == 60_000        # narrowest -> 1 min
    assert 60_000 < zoom_granularity(4*3600_000, 7*D, 6*3600_000) < 4*3600_000
    assert zoom_granularity(30*60_000, 20*60_000, 20*60_000) == 30*60_000  # axis shorter than MIN_SPAN -> picked


def test_zoom_granularity_steps_down_through_the_table_and_never_coarser():
    full = 30 * D
    sizes = [full, 20 * D, 10 * D, 3 * D, D, 12 * HOUR, 6 * HOUR, 3 * HOUR, HOUR, 45 * MIN, MIN_SPAN_MS]
    allowed = {g for _, _, g in GRANULARITIES}
    seen = [zoom_granularity(D, full, visible) for visible in sizes]
    assert seen[0] == D and seen[-1] == MIN
    assert all(g in allowed for g in seen)
    assert seen == sorted(seen, reverse=True)                   # zooming in only ever refines
    assert len(set(seen)) >= 4                                  # and passes through intermediate steps
    for picked in (g for _, _, g in GRANULARITIES):
        for visible in sizes:
            assert zoom_granularity(picked, full, visible) <= picked


def test_zoom_granularity_clamps_odd_windows():
    assert zoom_granularity(HOUR, 7 * D, 14 * D) == HOUR                 # wider than the axis
    assert zoom_granularity(HOUR, 7 * D, 1) == MIN                       # narrower than the minimum span
    assert zoom_granularity(HOUR, 7 * D, 0) == MIN
    assert zoom_granularity(MIN, 7 * D, 7 * D) == MIN                    # nothing finer than a minute
    assert zoom_granularity(MIN, 7 * D, MIN_SPAN_MS) == MIN
