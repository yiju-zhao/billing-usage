"""Time ranges, timeline buckets and the zoom rule for the billing-usage TUI.

Pure standard library, no I/O. Times are epoch milliseconds. Billing-cycle boundaries are 00:00 UTC;
"today", custom dates and bucket alignment use the process's local timezone (TZ, as libc sees it).
"""
import calendar
import math
import time
from datetime import date, timedelta
from typing import List, Optional, Tuple

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MINUTE_MS = 60_000

# (key, label) in the order the range selector shows them.
RANGES = [("today", "今天"), ("cycle", "本计费周期"), ("week", "自每周重置以来"), ("7d", "最近 7 天"),
          ("30d", "最近 30 天"), ("all", "全部"), ("custom", "自定义日期")]  # type: List[Tuple[str, str]]

# (key, label, milliseconds), finest first.
GRANULARITIES = [("1m", "1 分钟", MINUTE_MS), ("5m", "5 分钟", 5 * MINUTE_MS), ("15m", "15 分钟", 15 * MINUTE_MS),
                 ("30m", "30 分钟", 30 * MINUTE_MS), ("1h", "1 小时", 60 * MINUTE_MS),
                 ("4h", "4 小时", 240 * MINUTE_MS), ("1d", "1 天", DAY_MS)]  # type: List[Tuple[str, str, int]]

# Granularity a range switches to; "custom" is absent because it keeps the current one.
RANGE_BUCKETS = {"today": "30m", "week": "4h", "7d": "4h", "30d": "1d", "all": "1d", "cycle": "1d"}

# The narrowest window the timeline can be zoomed to.
MIN_SPAN_MS = 30 * MINUTE_MS

# Custom dates must fall in this window: usage starts after the epoch, and it keeps mktime well inside its range.
_FIRST_DAY = date(1970, 1, 1)
_LAST_DAY = date(2100, 12, 31)


def cycle_start_ms(now_ms: int, day: int) -> int:
    """The latest `YYYY-MM-<day>T00:00Z` that is not after `now_ms`. `day` is 1-28, so it exists every month."""
    now = time.gmtime(now_ms // 1000)
    year, month = now.tm_year, now.tm_mon
    start = calendar.timegm((year, month, day, 0, 0, 0)) * 1000
    if start > now_ms:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        start = calendar.timegm((year, month, day, 0, 0, 0)) * 1000
    return start


def _local_midnight_ms(day: date) -> int:
    """Epoch ms of 00:00 local time on `day`."""
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))) * 1000


def _local_date(t_ms: int) -> date:
    local = time.localtime(t_ms // 1000)
    return date(local.tm_year, local.tm_mon, local.tm_mday)


def _parse_day(text: str) -> date:
    """A user-typed `YYYY-MM-DD`; ValueError when malformed or outside the supported window."""
    day = date.fromisoformat(text)
    if not _FIRST_DAY <= day <= _LAST_DAY:
        raise ValueError("date out of range (%s to %s): %r" % (_FIRST_DAY, _LAST_DAY, text))
    return day


def range_bounds(key: str, now_ms: int, *, cycle_day: int, week_resets_at_ms: Optional[int],
                 custom_start: str, custom_end: str) -> Optional[Tuple[int, int]]:
    """`(from_ms, to_ms)` for a range key, `to_ms` exclusive; `None` for "week" before the reset time is known.

    `custom_start` and `custom_end` are local dates as `YYYY-MM-DD`; one that is malformed or outside
    1970-01-01..2100-12-31 raises ValueError. An empty start means local midnight today; an empty end means
    now. A non-empty end day is included whole.
    """
    if key == "today":
        return _local_midnight_ms(_local_date(now_ms)), now_ms
    if key == "7d":
        return now_ms - 7 * DAY_MS, now_ms
    if key == "30d":
        return now_ms - 30 * DAY_MS, now_ms
    if key == "all":
        return 0, now_ms
    if key == "cycle":
        return cycle_start_ms(now_ms, cycle_day), now_ms
    if key == "week":
        if week_resets_at_ms is None:
            return None
        # The reset time repeats every week, so the current period began at the latest
        # `resets_at + k*7d - 7d` that is not after now, whether resets_at is ahead of now or stale.
        start = week_resets_at_ms - WEEK_MS
        return start + (now_ms - start) // WEEK_MS * WEEK_MS, now_ms
    if key == "custom":
        start_day = _parse_day(custom_start) if custom_start else _local_date(now_ms)
        start = _local_midnight_ms(start_day)
        if not custom_end:
            return start, now_ms
        end = _local_midnight_ms(_parse_day(custom_end) + timedelta(days=1))
        return start, min(end, now_ms)
    raise ValueError("unknown range: %r" % (key,))


def bucket_start(t_ms: int, g_ms: int) -> int:
    """Start of the `g_ms` bucket holding `t_ms`, counted from local midnight of `t_ms`'s day.

    Counting from that day's real midnight keeps bucket starts increasing with `t_ms` on the 23- and 25-hour
    days when the clocks change, and a 1-day bucket is exactly one local day. On a 25-hour day the last bucket
    of a sub-day size is cut short by the next midnight.
    """
    midnight = _local_midnight_ms(_local_date(t_ms))
    if g_ms >= DAY_MS:
        return midnight
    return midnight + (t_ms - midnight) // g_ms * g_ms


def next_bucket_start(start_ms: int, g_ms: int) -> int:
    """Start of the bucket after the one holding `start_ms` (normally a `bucket_start` value).

    A 1-day bucket is followed by the next local midnight. A sub-day bucket is followed by `start + g_ms`, or
    by the next local midnight when that comes first (the last bucket of a 23-hour day ends at midnight).
    """
    start = bucket_start(start_ms, g_ms)
    next_midnight = _local_midnight_ms(_local_date(start) + timedelta(days=1))
    if g_ms >= DAY_MS:
        return next_midnight
    return min(start + g_ms, next_midnight)


def zoom_granularity(picked_ms: int, full_ms: int, visible_ms: int) -> int:
    """Bucket size for a zoomed timeline.

    Moves from `picked_ms` (axis fully shown) to one minute (axis at `MIN_SPAN_MS`) in proportion to the log of
    the zoom, then snaps to the nearest entry of GRANULARITIES in log scale. Never coarser than `picked_ms`.
    """
    if full_ms <= MIN_SPAN_MS:
        return picked_ms
    visible_ms = min(max(visible_ms, MIN_SPAN_MS), full_ms)
    t = math.log(full_ms / visible_ms) / math.log(full_ms / MIN_SPAN_MS)
    target = math.log(picked_ms) + t * (math.log(MINUTE_MS) - math.log(picked_ms))
    candidates = [g for _, _, g in GRANULARITIES if g <= picked_ms]
    if not candidates:
        return picked_ms
    return min(candidates, key=lambda g: abs(math.log(g) - target))
