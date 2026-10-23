import calendar
import time

import pytest

from usage_time import (GRANULARITIES, MIN_SPAN_MS, RANGE_BUCKETS, RANGES, bucket_start, cycle_start_ms,
                        next_bucket_start, range_bounds, zoom_granularity)

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


def test_week_range_rolls_a_past_reset_forward():
    now = ms("2026-10-22T05:00:00Z")
    # The reset time is stale (12 days ago): the current weekly period began on the latest weekly step <= now.
    assert bounds("week", now, week_resets_at_ms=ms("2026-10-10T10:00:00Z")) == (ms("2026-10-17T10:00:00Z"), now)
    # Same answer as the future-dated reset of the same weekly cycle.
    assert bounds("week", now, week_resets_at_ms=ms("2026-10-24T10:00:00Z")) == (ms("2026-10-17T10:00:00Z"), now)
    assert bounds("week", now, week_resets_at_ms=ms("2026-10-31T10:00:00Z")) == (ms("2026-10-17T10:00:00Z"), now)
    # Many weeks stale.
    assert bounds("week", now, week_resets_at_ms=ms("2026-01-02T10:00:00Z")) == (ms("2026-10-16T10:00:00Z"), now)
    # A reset that has just happened starts a new period at that moment.
    assert bounds("week", now, week_resets_at_ms=now) == (now, now)
    assert bounds("week", now, week_resets_at_ms=now - 1) == (now - 1, now)
    # A reset exactly a week ago: the period that ended then is over, the next one began then.
    assert bounds("week", now, week_resets_at_ms=now - 7 * D) == (now, now)
    # And one millisecond short of a week ahead: that period began 1 ms ago.
    assert bounds("week", now, week_resets_at_ms=now + 7 * D - 1) == (now - 1, now)


def test_week_range_start_is_always_within_the_last_week():
    now = ms("2026-10-22T05:00:00Z")
    for offset in range(-30 * D, 30 * D, 5 * HOUR + 7):
        start, end = bounds("week", now, week_resets_at_ms=now + offset)
        assert end == now
        assert now - 7 * D < start <= now
        assert (now + offset - 7 * D - start) % (7 * D) == 0       # always a whole number of weeks from the reset


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


@pytest.mark.parametrize("text", ["9999-12-31", "0001-01-01", "1969-12-31", "2101-01-01"])
def test_out_of_range_custom_date_is_a_value_error(text):
    now = ms("2026-10-22T05:00:00Z")
    with pytest.raises(ValueError):
        bounds("custom", now, custom_start=text, custom_end="")
    with pytest.raises(ValueError):
        bounds("custom", now, custom_start="", custom_end=text)
    with pytest.raises(ValueError):
        bounds("custom", now, custom_start="2026-10-01", custom_end=text)
    with pytest.raises(ValueError):
        bounds("custom", now, custom_start=text, custom_end="2026-10-20")


def test_custom_dates_at_the_edges_of_the_supported_window(tz_new_york):
    now = ms("2026-10-22T05:00:00Z")
    assert bounds("custom", now, custom_start="1970-01-01", custom_end="1970-01-01") == \
        (ms("1970-01-01T05:00:00Z"), ms("1970-01-02T05:00:00Z"))
    assert bounds("custom", now, custom_start="2100-12-31", custom_end="") == (ms("2100-12-31T05:00:00Z"), now)
    assert bounds("custom", now, custom_start="2026-10-01", custom_end="2100-12-31") == \
        (ms("2026-10-01T04:00:00Z"), now)


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


def test_next_bucket_start_local(tz_new_york):
    midnight = ms("2026-10-22T04:00:00Z")
    assert next_bucket_start(midnight, 4 * HOUR) == ms("2026-10-22T08:00:00Z")
    assert next_bucket_start(midnight, 30 * MIN) == ms("2026-10-22T04:30:00Z")
    assert next_bucket_start(midnight, MIN) == midnight + MIN
    assert next_bucket_start(midnight, D) == ms("2026-10-23T04:00:00Z")
    # The last bucket of a day is followed by the next midnight.
    assert next_bucket_start(ms("2026-10-23T00:00:00Z"), 4 * HOUR) == ms("2026-10-23T04:00:00Z")
    assert next_bucket_start(ms("2026-10-23T03:59:00Z"), MIN) == ms("2026-10-23T04:00:00Z")
    # A time inside a bucket stands for that bucket.
    assert next_bucket_start(ms("2026-10-22T07:10:00Z"), 4 * HOUR) == ms("2026-10-22T08:00:00Z")
    assert next_bucket_start(ms("2026-10-22T07:10:00Z"), D) == ms("2026-10-23T04:00:00Z")


def test_next_bucket_start_on_clock_change_days(tz_new_york):
    def walk(first, stop, g):
        keys = [first]
        while keys[-1] < stop:
            keys.append(next_bucket_start(keys[-1], g))
        return keys

    # Spring forward 2026-03-08: a 23-hour local day, 05:00Z to 04:00Z the next day.
    assert walk(ms("2026-03-08T05:00:00Z"), ms("2026-03-09T04:00:00Z"), 4 * HOUR) == [
        ms("2026-03-08T%sZ" % hhmm) for hhmm in ("05:00:00", "09:00:00", "13:00:00", "17:00:00", "21:00:00")
    ] + [ms("2026-03-09T01:00:00Z"), ms("2026-03-09T04:00:00Z")]
    assert walk(ms("2026-03-07T05:00:00Z"), ms("2026-03-10T04:00:00Z"), D) == [
        ms("2026-03-07T05:00:00Z"), ms("2026-03-08T05:00:00Z"), ms("2026-03-09T04:00:00Z"), ms("2026-03-10T04:00:00Z")]
    # Autumn back 2026-11-01: a 25-hour local day, 04:00Z to 05:00Z the next day; the last 4h bucket is cut short.
    assert walk(ms("2026-11-01T04:00:00Z"), ms("2026-11-02T05:00:00Z"), 4 * HOUR) == [
        ms("2026-11-01T04:00:00Z"), ms("2026-11-01T08:00:00Z"), ms("2026-11-01T12:00:00Z"),
        ms("2026-11-01T16:00:00Z"), ms("2026-11-01T20:00:00Z"), ms("2026-11-02T00:00:00Z"),
        ms("2026-11-02T04:00:00Z"), ms("2026-11-02T05:00:00Z")]
    assert walk(ms("2026-10-31T04:00:00Z"), ms("2026-11-03T05:00:00Z"), D) == [
        ms("2026-10-31T04:00:00Z"), ms("2026-11-01T04:00:00Z"), ms("2026-11-02T05:00:00Z"), ms("2026-11-03T05:00:00Z")]


@pytest.mark.parametrize("first,stop", [("2026-03-07T05:00:00Z", "2026-03-10T04:00:00Z"),
                                        ("2026-10-31T04:00:00Z", "2026-11-03T05:00:00Z")])
def test_next_bucket_start_walks_exactly_the_buckets_that_hold_data(tz_new_york, first, stop):
    first, stop = ms(first), ms(stop)
    for g_ms in (g for _, _, g in GRANULARITIES):
        keys = [bucket_start(first, g_ms)]
        while keys[-1] < stop:
            keys.append(next_bucket_start(keys[-1], g_ms))
        assert all(b > a for a, b in zip(keys, keys[1:]))                       # strictly increasing
        assert all(bucket_start(k, g_ms) == k for k in keys)                    # every key is a bucket start
        scanned = sorted({bucket_start(t, g_ms) for t in range(first, stop, MIN)})
        assert keys == scanned + [stop]                                         # none skipped, none invented


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
