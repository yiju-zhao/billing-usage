"""Where the billing-usage TUI keeps things: paths, per-device row caches, saved choices, the status-line
summary, and the parallel refresh of every device.

Standard library only; local use (the script is never sent to ssh devices), but kept Python 3.8 clean.
Times are epoch milliseconds unless a name says seconds. Every file is written through atomic_write_json,
so a reader (the status line) never sees half a file.
"""
import concurrent.futures
import copy
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from billing_usage import fetch_device_rows, hint_for
from usage_groups import GROUPINGS, METRICS, fmt_usd, total
from usage_time import GRANULARITIES, RANGES, cycle_start_ms, range_bounds

Row = dict  # see the plan's shared row shape; the TUI adds "device"

DEFAULT_STATE = {
    "cycle_start_day": 8,
    "timeline": {"metric": "cost", "device": "all", "by": "model", "granularity": "4h", "range": "7d",
                 "start": "", "end": ""},
    "share": {"metric": "cost", "device": "all", "by": "project", "range": "cycle", "start": "", "end": ""},
}

ALL_TIME_START = "2000-01-01"  # what fetch_start asks for when a section shows "all"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Allowed values of each state key: a set of option keys, or str for "any string".
_CHOICES = {
    "metric": set(METRICS),
    "by": set(GROUPINGS),
    "range": {key for key, _ in RANGES},
    "granularity": {key for key, _, _ in GRANULARITIES},
    "device": str,
    "start": str,
    "end": str,
}


# ---------------------------------------------------------------- paths and atomic writes

def _xdg(variable: str, fallback: str) -> Path:
    # An empty variable counts as unset, as the XDG spec says.
    return Path(os.environ.get(variable) or os.path.expanduser(fallback)) / "billing-usage"


def cache_dir() -> Path:
    """`${XDG_CACHE_HOME:-~/.cache}/billing-usage`, read from the environment at each call."""
    return _xdg("XDG_CACHE_HOME", "~/.cache")


def state_path() -> Path:
    """`${XDG_STATE_HOME:-~/.local/state}/billing-usage/tui.json`, read from the environment at each call."""
    return _xdg("XDG_STATE_HOME", "~/.local/state") / "tui.json"


def atomic_write_json(path, obj) -> None:
    """Write `obj` as JSON so that `path` holds either the old or the new content, never a mixture.

    Creates missing parent directories. Raises (and leaves `path` alone) when `obj` is not serialisable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(temp, str(path))  # temp is in the same directory, so this is one atomic rename
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_json(path) -> Any:
    """The parsed file, or None when it is missing, unreadable or not JSON."""
    try:
        with open(str(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, RecursionError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None


# ---------------------------------------------------------------- saved choices

def _valid(key: str, value: Any) -> bool:
    allowed = _CHOICES[key]
    if allowed is str:
        return isinstance(value, str)
    return isinstance(value, str) and value in allowed


def load_state() -> dict:
    """The saved choices over DEFAULT_STATE. Each missing or invalid value falls back to its default on its
    own; a file that cannot be read or parsed gives all defaults. Always a fresh dict, safe to edit."""
    state = copy.deepcopy(DEFAULT_STATE)
    saved = _read_json(state_path())
    if not isinstance(saved, dict):
        return state
    day = saved.get("cycle_start_day")
    if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 28:
        state["cycle_start_day"] = day
    for name in ("timeline", "share"):
        section = saved.get(name)
        if isinstance(section, dict):
            for key in state[name]:
                if key in section and _valid(key, section[key]):
                    state[name][key] = section[key]
    return state


def save_state(state: dict) -> None:
    atomic_write_json(state_path(), state)


def read_week_resets_at_ms() -> Optional[int]:
    """When the 7-day limit next resets, from the status line's `limits.json`
    (`{"seven_day_resets_at": <epoch seconds>}`); None when it is absent or not a plausible time."""
    data = _read_json(cache_dir() / "limits.json")
    value = data.get("seven_day_resets_at") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return int(round(value * 1000))


# ---------------------------------------------------------------- per-device rows

@dataclass
class DeviceResult:
    name: str
    rows: List[Row]
    ok: bool  # this device was read in the latest refresh
    error: Optional[str] = None  # why it could not be read
    hint: Optional[str] = None  # what to do about it (billing_usage.HINTS)
    last_ok_ms: Optional[int] = None  # when `rows` were fetched; None when there are none
    unpriced: List[str] = field(default_factory=list)  # models in `rows` that have no price


def _cache_file(name: str) -> Path:
    encoded = _UNSAFE.sub(lambda m: "".join("%%%02X" % b for b in m.group().encode("utf-8")), name)
    return cache_dir() / ("rows-%s.json" % encoded)


def _tag(rows: List[Row], name: str) -> List[Row]:
    return [dict(r, device=name) for r in rows]


def _read_cache(name: str):
    """(rows, unpriced, fetched_at_ms) from the device's cache file, or None when it is missing or damaged."""
    data = _read_json(_cache_file(name))
    if not isinstance(data, dict):
        return None
    rows, fetched_at, unpriced = data.get("rows"), data.get("fetched_at"), data.get("unpriced", [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, int):
        return None
    return _tag(rows, name), unpriced if isinstance(unpriced, list) else [], fetched_at


def load_cached(devices: List[dict]) -> List[DeviceResult]:
    """The last good rows of each device, for an instant first screen: `ok` is False and `error` None until
    the first refresh has run. A device without a usable cache gets no rows."""
    results = []
    for dev in devices:
        cached = _read_cache(dev["name"])
        rows, unpriced, fetched_at = cached if cached else ([], [], None)
        results.append(DeviceResult(dev["name"], rows, False, None, None, fetched_at, unpriced))
    return results


def _refresh_device(dev: dict, start: str, timeout: int, config_dir: str, fetch: Callable, now_ms: int) -> DeviceResult:
    name = dev["name"]
    try:
        data = fetch(dev, start, None, timeout, config_dir)
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list) \
                or not all(isinstance(r, dict) for r in data["rows"]):
            raise ValueError("malformed result from the device")
        unpriced = data.get("unpriced")
        unpriced = list(unpriced) if isinstance(unpriced, list) else []
        rows = _tag(data["rows"], name)
    except Exception as e:  # any failure makes this device offline; the others carry on
        error = str(e) or type(e).__name__
        cached = _read_cache(name)
        rows, unpriced, fetched_at = cached if cached else ([], [], None)
        return DeviceResult(name, rows, False, error, hint_for(dev, error), fetched_at, unpriced)
    try:
        atomic_write_json(_cache_file(name), {"fetched_at": now_ms, "start": start, "rows": rows,
                                              "unpriced": unpriced})
    except OSError:
        pass  # the fresh rows are still good; only the offline fallback is lost
    return DeviceResult(name, rows, True, None, None, now_ms, unpriced)


def refresh_all(devices: List[dict], start: str, timeout: int, config_dir: str,
                fetch: Callable = fetch_device_rows, now_ms: Optional[int] = None) -> List[DeviceResult]:
    """Read every device at once (one thread each), in `devices` order.

    A device that reads fine gets its rows tagged with its name and cached to `rows-<name>.json`. One that
    cannot be read is `ok=False` with the reason, a hint, and its last cached rows (if any), so a single
    offline device never blanks the screen. `fetch(dev, start, end, timeout, config_dir)` is injectable.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if not devices:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = [pool.submit(_refresh_device, dev, start, timeout, config_dir, fetch, now_ms) for dev in devices]
        return [f.result() for f in futures]


# ---------------------------------------------------------------- fetch window

def _utc_date(t_ms: int) -> str:
    return (_EPOCH + timedelta(milliseconds=t_ms)).strftime("%Y-%m-%d")


def fetch_start(state: dict, now_ms: int) -> str:
    """The UTC date (`YYYY-MM-DD`) to fetch from: the earliest start among this cycle, both sections' ranges and
    today. `ALL_TIME_START` when a section shows "all". A range that cannot be resolved (bad custom dates, "week"
    before its reset time is known) adds nothing."""
    cycle_day = state["cycle_start_day"]
    week_resets_at_ms = read_week_resets_at_ms()
    starts = [cycle_start_ms(now_ms, cycle_day)]
    keys = [("today", "", "")] + [(s.get("range"), s.get("start", ""), s.get("end", ""))
                                  for s in (state["timeline"], state["share"])]
    for key, custom_start, custom_end in keys:
        if key == "all":
            return ALL_TIME_START
        try:
            bounds = range_bounds(key, now_ms, cycle_day=cycle_day, week_resets_at_ms=week_resets_at_ms,
                                  custom_start=custom_start, custom_end=custom_end)
        except ValueError:
            continue
        if bounds is not None:
            starts.append(bounds[0])
    return _utc_date(min(starts))


# ---------------------------------------------------------------- status-line summary

def _cost_between(rows: List[Row], bounds) -> float:
    first, last = bounds
    return total((r for r in rows if first <= r["t"] < last), "cost")


def write_summary(results: List[DeviceResult], state: dict, now_ms: int) -> Dict[str, Any]:
    """Write `summary.json` for the status line and return what it holds.

    Costs add up every device's rows, an offline device's cached rows included (that is what the TUI shows);
    `devices_ok` of `devices_total` tells the reader how many were read in the latest refresh. Raises OSError
    when the file cannot be written.
    """
    rows = [r for result in results for r in result.rows]
    bounds = dict(cycle_day=state["cycle_start_day"], week_resets_at_ms=None, custom_start="", custom_end="")
    today_cost = _cost_between(rows, range_bounds("today", now_ms, **bounds))
    cycle_cost = _cost_between(rows, range_bounds("cycle", now_ms, **bounds))
    summary = {
        "today_cost": today_cost,
        "cycle_cost": cycle_cost,
        "today_text": fmt_usd(today_cost),
        "cycle_text": fmt_usd(cycle_cost),
        "devices_ok": sum(1 for r in results if r.ok),
        "devices_total": len(results),
        "updated_at": now_ms // 1000,
    }
    atomic_write_json(cache_dir() / "summary.json", summary)
    return summary
