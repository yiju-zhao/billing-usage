# billing-usage TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A resident terminal UI, modelled on token-meter's sidebar, that shows Claude Code usage and list-price cost across every device in the billing-usage devices file, with no model calls, plus a "今日 / 周期" segment in the user's Claude Code status line.

**Architecture:** `billing_usage.py` (stdlib, also shipped to ssh devices on stdin) gains a per-request `--rows` mode built on the same dedup and pricing code as the report. Two pure stdlib modules turn rows into time ranges, buckets, groups and colours; a small store module fetches devices in parallel, falls back to cached rows for unreachable ones, and writes the status-line summary. A Textual app renders two token-meter-style sections (timeline, share table) driven by sentence pickers.

**Tech Stack:** Python 3.13 locally (remote `python3` unknown version), Textual (pinned in a PEP 723 header, run with `uv run --script`), pytest via `uv run --with`, bash + jq for the status line.

**Spec:** `docs/superpowers/specs/2026-10-22-billing-usage-tui-design.md` (Chinese). Read it before any task; UI copy below comes from it verbatim.

## Global Constraints

- `scripts/billing_usage.py` stays standard-library only and must run under Python 3.8 on remote devices: no `match`, no `X | Y` annotations evaluated at runtime, no `str.removeprefix`, no 3.9+ stdlib APIs.
- The report's default output and `--json` output must be byte-identical to today's for the same transcripts (the report skill must not change).
- Nothing is installed or written on ssh devices. ssh options stay `BatchMode=yes`, `ConnectTimeout=10`.
- TUI cycle totals (cost, requests, every token field) must equal `billing_usage.py --start <cycle start>` TOTAL for the same devices at the same moment.
- Paths: cache `${XDG_CACHE_HOME:-~/.cache}/billing-usage/`; state `${XDG_STATE_HOME:-~/.local/state}/billing-usage/tui.json`; devices file default unchanged (`${XDG_CONFIG_HOME:-~/.config}/billing-usage/devices.json`).
- Refresh every 60 s, `r` refreshes now; never start a refresh while one is in flight.
- `cycle_start_day` default 8, allowed 1–28, cycle boundaries at 00:00 UTC. Timeline buckets, "今天" and custom dates use local time.
- Glyphs: only `█ ░ ▁ ▂ ▃ ▄ ▅ ▆ ▇` plus box-drawing lines (Ghostty + JetBrainsMono Nerd Font).
- Colours: truecolor, the six ramps and `#5b5e66` grey exactly as in the spec's 颜色 section.
- All user-facing copy in Chinese, as in the spec's mockup.
- Status-line addition: jq only, < 50 ms, no Python, no ssh, silent when the summary file is absent.
- Run tests from the repo root: `uv run --with pytest --with textual==<pinned> pytest plugins/billing-usage/tests -q`.

## Review Focus

1. A remote device whose `python3` is older than the Mac's: `--rows` must still run there. → Task 1 runs the script under Python 3.8.
2. Transcript rows that are malformed, missing `cwd`/`effort`/`attributionSkill`, `<synthetic>`, or a model with no price: they are skipped or default to `"none"`/`unknown`/`cost: null`, never crash. → Task 1 fixture.
3. A range with zero requests (fresh cycle, quiet device): totals 0, shares 0, no division by zero, UI shows "无数据". → Task 3 and Task 5.
4. Cache or state file truncated by a crash, or written by an older version: ignored, defaults used, rewritten atomically. → Task 4.
5. A terminal narrower than 100 columns, or resized while running: layout compresses, no exception. → Task 5 pilot at 70×20.

---

## File Structure

```
plugins/billing-usage/
  scripts/
    billing_usage.py        modify: shared collect, request_rows, summarize_rows, --rows, fetch_device_rows
    usage_time.py           create: ranges, cycle start, buckets, granularities, zoom rule (stdlib, pure)
    usage_groups.py         create: metrics, grouping, segments + 其他, colours, prices, number formats (stdlib, pure)
    usage_store.py          create: paths, cached rows, state, summary.json, limits.json, refresh_all (stdlib)
    billing_usage_tui.py    create: PEP 723 header, Textual app, --once
    tui_widgets.py          create: SentenceBar, TimelineChart (+ pure render_bars), Legend, ShareTable
  tests/
    conftest.py             create: sys.path to scripts/, transcript fixture builder
    test_rows.py  test_time.py  test_groups.py  test_store.py  test_tui.py  test_render.py
README.md                   modify: billing-usage section
~/.claude/scripts/statusline.sh   modify (outside the repo; back up first)
```

Shared row shape (produced by Task 1, used everywhere):

```python
Row = dict  # {"t": int ms, "model": str, "effort": str, "project": str, "skill": str,
            #  "input": int, "output": int, "cache_write": int, "cache_write_1h": int,
            #  "cache_read": int, "cost": float | None, "premium": bool}
# The TUI adds "device": str after fetching.
```

---

### Task 1: Per-request rows in `billing_usage.py`

**Files:**
- Modify: `plugins/billing-usage/scripts/billing_usage.py` (`collect`, `summarize`, `run_ssh`, `run_device`, `main`)
- Create: `plugins/billing-usage/tests/conftest.py`, `plugins/billing-usage/tests/test_rows.py`

**Interfaces:**
- Produces:
  - `collect(projects_dir, start, end) -> list` of `[first_ts: datetime, usage: dict, model: str|None, meta: dict]`; `meta = {"effort", "cwd", "skill"}` taken from the row whose usage is kept. Existing callers keep working with the first three items.
  - `request_rows(collected) -> list[Row]`
  - `summarize_rows(config_dir, start, end) -> {"rows": list[Row], "unpriced": list[str]}`
  - CLI `--rows`: prints `summarize_rows(...)` as one JSON line (same arguments as `--json`)
  - `fetch_remote(dev, flag: str, start: str, end: str|None, timeout: int) -> dict` (the body of today's `run_ssh`, parameterised by `--json`/`--rows`); `run_ssh(dev, a)` becomes `fetch_remote(dev, "--json", a.start, a.end, a.timeout)`
  - `fetch_device_rows(dev, start: str, end: str|None, timeout: int, config_dir: str) -> dict` — local: `summarize_rows`; ssh: `fetch_remote(dev, "--rows", ...)`. Raises on failure (callers use `hint_for`).
  - `conftest.write_transcript(dir, rows: list[dict]) -> path` and fixture `projects_dir` building the transcript set below.

- [ ] **Step 1: Write the fixture and failing tests**

The fixture (`projects_dir`) writes under `tmp_path/projects/-x/` one jsonl with: request A streamed three times (same `message.id` + `requestId`, output 1, 5, 40; earliest timestamp `2026-10-08T00:30:00Z`), with `cwd: "/Users/u/Repos/NASA718"`, `effort: "xhigh"`, `attributionSkill: "investigate"`; request B (`claude-sonnet-5-5`, no `effort`, no `attributionSkill`, `isSidechain: true`, cwd `/a/b/career-ops`); request C with model `claude-unknown-9`; a `<synthetic>` row; a `user` row; a malformed line; request D at `2026-10-07T23:59:59Z` (before the window).

```python
def test_rows_dedup_and_metadata(projects_dir):
    out = summarize_rows(str(projects_dir.parent), START_1008, END_1009)
    a = [r for r in out["rows"] if r["skill"] == "investigate"]
    assert len(a) == 1 and a[0]["output"] == 40
    assert a[0]["t"] == ms("2026-10-08T00:30:00Z")
    assert (a[0]["project"], a[0]["effort"]) == ("nasa718", "xhigh")
    b = next(r for r in out["rows"] if r["model"] == "claude-sonnet-5-5")
    assert (b["effort"], b["skill"], b["project"]) == ("none", "none", "career-ops")
    assert out["unpriced"] == ["claude-unknown-9"]
    assert next(r for r in out["rows"] if r["model"] == "claude-unknown-9")["cost"] is None
    assert len(out["rows"]) == 3          # D, synthetic, user, malformed excluded

def test_rows_sum_equals_report_tally(projects_dir):
    rows = summarize_rows(str(projects_dir.parent), START_1008, END_1009)["rows"]
    days = summarize(str(projects_dir.parent), START_1008, END_1009)["days"]
    total = _total(days.values())
    assert sum(r["cost"] or 0 for r in rows) == pytest.approx(total["cost"], abs=1e-9)
    assert sum(r["input"] for r in rows) == total["input_tokens"]
    assert sum(r["cache_write"] for r in rows) == total["cache_creation_input_tokens"]
    assert len(rows) == total["requests"]

def test_report_output_unchanged(projects_dir, capsys):
    # golden captured from the ORIGINAL script on the same fixture before editing (Step 2)
    assert run_cli(["--start", "2026-10-08", "--end", "2026-10-08", "--devices", local_devices_file]) == GOLDEN_REPORT
    assert run_cli([... "--json"]) == GOLDEN_JSON

def test_rows_cli_runs_on_python38(projects_dir):
    out = subprocess.run(["uv", "run", "--python", "3.8", "--no-project", "python", SCRIPT, "--rows",
                          "--start", "2026-10-08", "--config-dir", str(projects_dir.parent)], ...)
    assert json.loads(out.stdout.splitlines()[-1])["rows"]
# If uv cannot provide 3.8 on this machine, use the oldest Python it can and say which in the commit message.
```

- [ ] **Step 2: Capture the golden outputs from the unmodified script, then run the tests**

Run the original script on the fixture with and without `--json`, save the outputs as `tests/golden_report.txt` and `tests/golden_json.txt`. The report header prints `now`-dependent text only when `--end` is absent, so the golden runs always pass `--end`.
Run: `uv run --with pytest pytest plugins/billing-usage/tests/test_rows.py -q`
Expected: FAIL — `summarize_rows` not defined.

- [ ] **Step 3: Implement the interfaces above in `billing_usage.py`**

`project` = last path component of `cwd`, lower-cased, `"unknown"` when absent. `effort` = row `effort` or `"none"`. `skill` = `attributionSkill` or `"none"`. `premium` uses today's rule (`speed == "fast"` or `inference_geo == "us"`). `cache_write_1h` = `usage.cache_creation.ephemeral_1h_input_tokens` or 0. `cost` = `cost_of(model, usage)`. Keep JSON keys exactly as in `Row`.

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest pytest plugins/billing-usage/tests/test_rows.py -q`
Expected: PASS (4 tests). Also run `/billing-usage:report`'s command by hand on this machine and diff against a run of the version on `main`: identical.

- [ ] **Step 5: Commit**

```bash
git add plugins/billing-usage/scripts/billing_usage.py plugins/billing-usage/tests
git commit -m "feat(billing-usage): per-request --rows mode sharing the report's dedup and pricing"
```

---

### Task 2: Time ranges, buckets and zoom (`usage_time.py`)

**Files:**
- Create: `plugins/billing-usage/scripts/usage_time.py`, `plugins/billing-usage/tests/test_time.py`

**Interfaces:**
- Produces:
  - `RANGES: list[tuple[str, str]]` = `[("today","今天"),("cycle","本计费周期"),("week","自每周重置以来"),("7d","最近 7 天"),("30d","最近 30 天"),("all","全部"),("custom","自定义日期")]`
  - `GRANULARITIES: list[tuple[str, str, int]]` = `1m 1 分钟 60000`, `5m 5 分钟`, `15m 15 分钟`, `30m 30 分钟`, `1h 1 小时`, `4h 4 小时`, `1d 1 天`
  - `RANGE_BUCKETS = {"today":"30m","week":"4h","7d":"4h","30d":"1d","all":"1d","cycle":"1d"}` (custom: keep current)
  - `cycle_start_ms(now_ms: int, day: int) -> int` — latest `YYYY-MM-<day>T00:00Z` ≤ now
  - `range_bounds(key, now_ms, *, cycle_day, week_resets_at_ms, custom_start, custom_end) -> tuple[int, int] | None` — `None` only for `"week"` when `week_resets_at_ms` is None; `week` = `resets_at − 7 d` (if that is in the future, step back 7 d); `all` = `(0, now)`; custom end = next local midnight after `custom_end`, capped at now; empty `custom_end` = now
  - `bucket_start(t_ms: int, g_ms: int) -> int` — aligned to local midnight (local UTC offset at `t`)
  - `MIN_SPAN_MS = 30 * 60_000`
  - `zoom_granularity(picked_ms, full_ms, visible_ms) -> int`

- [ ] **Step 1: Write failing tests**

```python
def test_cycle_start():
    assert cycle_start_ms(ms("2026-10-22T05:00:00Z"), 8) == ms("2026-10-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-10-08T00:00:00Z"), 8) == ms("2026-10-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-10-07T23:59:59Z"), 8) == ms("2026-09-08T00:00:00Z")
    assert cycle_start_ms(ms("2026-01-03T00:00:00Z"), 8) == ms("2025-12-08T00:00:00Z")

def test_week_range(): ...   # resets_at 2026-10-24T10:00Z, now 10-22 → from 10-17T10:00Z; resets None → None
def test_today_and_custom_local(tz_new_york): ...  # local midnight in America/New_York via time.tzset; custom end inclusive of that day
def test_bucket_start_local(tz_new_york):
    t = ms("2026-10-22T07:10:00Z")            # 03:10 EDT
    assert bucket_start(t, 4 * 3600_000) == ms("2026-10-22T04:00:00Z")   # 00:00 EDT
    assert bucket_start(t, 30 * 60_000) == ms("2026-10-22T07:00:00Z")
def test_zoom_granularity():
    D = 86400_000
    assert zoom_granularity(4*3600_000, 7*D, 7*D) == 4*3600_000          # unzoomed → picked
    assert zoom_granularity(4*3600_000, 7*D, MIN_SPAN_MS) == 60_000        # narrowest → 1 min
    assert 60_000 < zoom_granularity(4*3600_000, 7*D, 6*3600_000) < 4*3600_000
    assert zoom_granularity(30*60_000, 20*60_000, 20*60_000) == 30*60_000  # axis shorter than MIN_SPAN → picked
```

- [ ] **Step 2: Run** `uv run --with pytest pytest plugins/billing-usage/tests/test_time.py -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `usage_time.py`**

Zoom rule (token-meter `timeline.js`): if `full ≤ MIN_SPAN` return picked; `t = min(1, ln(full/visible) / ln(full/MIN_SPAN))`; `target = ln(picked) + t·(ln(60000) − ln(picked))`; among granularities `≤ picked`, return the one whose `ln(ms)` is nearest `target`.

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(billing-usage): time ranges, local buckets and zoom rule"`

---

### Task 3: Metrics, groups, colours, formats (`usage_groups.py`)

**Files:**
- Create: `plugins/billing-usage/scripts/usage_groups.py`, `plugins/billing-usage/tests/test_groups.py`

**Interfaces:**
- Consumes: `Row` (with `"device"`), `billing_usage.PRICES`
- Produces:
  - `METRICS = {"cost": "花费", "tokens": "tokens", "requests": "请求数"}`; `GROUPINGS = {"model": "按模型", "project": "按项目", "device": "按设备", "skill": "按 skill"}`
  - `row_tokens(r) -> int` = input + output + cache_read + cache_write
  - `@dataclass Group: key: str; label: str; color: str; requests: int; tokens: int; cost: float; input: int; output: int; cache_read: int; cache_write: int; model: str|None; effort: str|None; members: int = 1; other: bool = False` with `value(metric) -> float`
  - `aggregate(rows, by) -> list[Group]` — one per distinct key; by model the key is `(model, effort)` and order is slot order then effort order; otherwise largest cost first
  - `segments(rows, by, metric) -> list[Group]` — by model: all; else top 6 by metric, rest merged into one `Group(label=f"其他 · {n} 个", color="#5b5e66", other=True)` unless the rest is a single group; drop groups under 0.1% of the metric total (they still count in `total(rows, metric)`)
  - `total(rows, metric) -> float`
  - `SLOT_RAMPS`, `GREY = "#5b5e66"`, `EFFORTS = ["low","medium","high","xhigh","max"]`, `MODEL_SLOTS = ["claude-opus-5-5","claude-sonnet-5-5","claude-haiku-5-5","claude-fable-5-1","claude-opus-5","claude-sonnet-5"]`
  - `model_slots(models: list[str]) -> dict[str, int]`, `model_color(model, effort, slots) -> str`, `name_color(name: str) -> str`
  - `model_label(model, effort) -> str` — drop `claude-` prefix, append effort unless `"none"`: `"opus-5-5 xhigh"`
  - `price_of(model) -> tuple|None` — exact key, else longest `PRICES` key that `model` starts with followed by `-`
  - `fmt_usd(x) -> str`, `fmt_num(n) -> str`, `fmt_pct(part, whole) -> str`

- [ ] **Step 1: Write failing tests**

```python
def test_name_color_matches_token_meter():
    # h = 7; h = (h*31 + ord(c)) mod 2**32; ramp[h % 6][4]
    assert name_color("nasa718") == SLOT_RAMPS[_h("nasa718") % 6][4]
    assert name_color("NASA718") == name_color("nasa718")
def test_model_colors():
    slots = model_slots(["claude-opus-5-5", "claude-zeta-1", "claude-alpha-1"])
    assert model_color("claude-opus-5-5", "high", slots) == "#3987e5"
    assert model_color("claude-opus-5-5", "low", slots) == "#184f95"
    assert model_color("claude-opus-5-5", "none", slots) == "#5b5e66"
    assert slots["claude-alpha-1"] == 6 % 6 and slots["claude-zeta-1"] == 7 % 6   # spares by name order after the 6 listed
    assert model_color("claude-opus-5-5-20261001", "high", model_slots(["claude-opus-5-5-20261001"])) == "#3987e5"
def test_segments_other_rule(): ...   # 9 projects → 6 + "其他 · 3 个" grey; 7 projects → all 7, no 其他; a 0.05% project absent but in total
def test_empty_rows():
    assert aggregate([], "project") == [] and segments([], "model", "cost") == [] and total([], "cost") == 0
    assert fmt_pct(0, 0) == "0%"
def test_formats():
    assert [fmt_usd(x) for x in (97.94, 258.2, 0.2961, 4350, 0)] == ["$97.9", "$258", "$0.296", "$4.35K", "$0"]
    assert [fmt_num(n) for n in (999, 1234, 3_400_000, 5_600_000_000)] == ["999", "1.2K", "3.4M", "5.6B"]
def test_price_of():
    assert price_of("claude-opus-5-5-20261001") == PRICES["claude-opus-5-5"]
    assert price_of("claude-opus-5") == PRICES["claude-opus-5"]     # not captured by opus-5-5
```

- [ ] **Step 2: Run** `uv run --with pytest pytest plugins/billing-usage/tests/test_groups.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement `usage_groups.py`** (ramps verbatim from the spec; `members` counts merged groups).
- [ ] **Step 4: Run tests** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(billing-usage): grouping, fixed colours and number formats"`

---

### Task 4: Fetching, cache, state and summary (`usage_store.py`)

**Files:**
- Create: `plugins/billing-usage/scripts/usage_store.py`, `plugins/billing-usage/tests/test_store.py`

**Interfaces:**
- Consumes: `billing_usage.load_devices`, `fetch_device_rows`, `hint_for`; `usage_time.range_bounds`, `cycle_start_ms`; `usage_groups.fmt_usd`
- Produces:
  - `cache_dir() -> Path`, `state_path() -> Path` (XDG paths above, read at call time)
  - `atomic_write_json(path, obj) -> None` (temp file in the same dir + `os.replace`)
  - `DEFAULT_STATE = {"cycle_start_day": 8, "timeline": {"metric":"cost","device":"all","by":"model","granularity":"4h","range":"7d","start":"","end":""}, "share": {"metric":"cost","device":"all","by":"project","range":"cycle","start":"","end":""}}`
  - `load_state() -> dict` — deep-merges the file over `DEFAULT_STATE`; unreadable/invalid/out-of-range values fall back to defaults; `save_state(state) -> None`
  - `read_week_resets_at_ms() -> int|None` — from `cache_dir()/limits.json` `{"seven_day_resets_at": <epoch seconds>}`
  - `@dataclass DeviceResult: name: str; rows: list[Row]; ok: bool; error: str|None; hint: str|None; last_ok_ms: int|None; unpriced: list[str]`
  - `refresh_all(devices, start: str, timeout: int, config_dir: str, fetch=fetch_device_rows, now_ms=None) -> list[DeviceResult]` — parallel; adds `"device"` to rows; on success writes `cache_dir()/rows-<name>.json` `{"fetched_at": ms, "start": str, "rows": [...], "unpriced": [...]}`; on failure returns cached rows (if any) with `ok=False`, `error`, `hint_for(...)`, `last_ok_ms = fetched_at`
  - `load_cached(devices) -> list[DeviceResult]` — for instant startup, all `ok=False, error=None` until the first refresh
  - `fetch_start(state, now_ms) -> str` — UTC `YYYY-MM-DD` of the earliest start any of: cycle start, both sections' ranges, today; `"2000-01-01"` when a section uses `all`
  - `write_summary(results, state, now_ms) -> dict` — writes `cache_dir()/summary.json` `{"today_cost", "cycle_cost", "today_text", "cycle_text", "devices_ok", "devices_total", "updated_at": <epoch seconds>}`

- [ ] **Step 1: Write failing tests** (all set `XDG_CACHE_HOME`/`XDG_STATE_HOME` to `tmp_path`)

```python
def test_offline_device_uses_cache(tmp_path):
    devs = [{"name": "a", "method": "local"}, {"name": "b", "method": "ssh", "address": "x"}]
    ok_fetch = lambda d, *a, **k: {"rows": [row(cost=1.0)], "unpriced": []}
    refresh_all(devs, "2026-10-08", 5, "~/.claude", fetch=ok_fetch, now_ms=T0)
    def flaky(d, *a, **k):
        if d["name"] == "b": raise RuntimeError("ssh: connect to host x port 22: Connection refused")
        return ok_fetch(d)
    res = refresh_all(devs, "2026-10-08", 5, "~/.claude", fetch=flaky, now_ms=T1)
    b = next(r for r in res if r.name == "b")
    assert not b.ok and b.rows and b.rows[0]["device"] == "b" and b.last_ok_ms == T0
    assert "Remote Login" in b.hint
def test_corrupt_files_fall_back(tmp_path):
    (state_path()).parent.mkdir(parents=True); state_path().write_text('{"cycle_start_day": 99, "timeline": ')
    assert load_state() == DEFAULT_STATE
    (cache_dir() / "rows-a.json").write_text("{not json")
    assert load_cached([{"name": "a", "method": "local"}])[0].rows == []
def test_summary(tmp_path): ...   # today_cost counts local-midnight..now, cycle_cost counts cycle start..now; texts via fmt_usd; devices_ok counts ok
def test_fetch_start(): ...       # 7d + cycle(8th) on 10-22 → "2026-10-08"; a section on "all" → "2000-01-01"
```

- [ ] **Step 2: Run** `uv run --with pytest pytest plugins/billing-usage/tests/test_store.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement `usage_store.py`.**
- [ ] **Step 4: Run tests** — Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(billing-usage): parallel device refresh with cached fallback and status-line summary"`

---

### Task 5: The app shell, sentences and share table

**Files:**
- Create: `plugins/billing-usage/scripts/billing_usage_tui.py`, `plugins/billing-usage/scripts/tui_widgets.py`, `plugins/billing-usage/tests/test_tui.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `billing_usage_tui.py`: PEP 723 header (`requires-python = ">=3.11"`, `dependencies = ["textual==<latest release on PyPI at implementation time>"]`), `class UsageApp(App)` with `__init__(self, devices, *, config_dir, timeout=120, fetch=fetch_device_rows, refresh_s=60)`, `main()` with flags `--devices`, `--config-dir`, `--timeout`, `--once` (refresh, write summary, print `本周期 <cycle_text> · 今日 <today_text> · N/M 台在线` plus one line per offline device, exit)
  - `tui_widgets.py`: `SentenceBar(section: str, fields: list[tuple[field, options]])` posting `SentenceBar.Changed(section, field, value)`, total shown right-aligned via `set_total(text)`; `ShareTable` with `update(groups, metric, by, total)`
- App behaviour:
  - Header line: `Billing Usage   今日 $X · 本周期 $Y · N/M 台在线 · K 分钟前刷新      r 刷新  q 退出`; when a device is offline, a second line per device: `<name> 离线 · 最后成功于 HH:MM · <hint>` (or `· 从未成功`); unpriced models: `未定价：<models>（tokens 计入，花费按 0）`; premium requests in the shown ranges: `<n> 次请求用了快速模式或仅美国推理（花费偏低）` (same warnings as the report).
  - Sentences: timeline `指标 · 设备 · 分组 · 每<粒度> · 范围`, share `指标 · 设备 · 分组 · 范围`; device options `所有设备` + device names. Words are clickable and reachable by Tab, Enter opens the choices. Choosing `custom` shows two date inputs (YYYY-MM-DD). `week` is disabled with `（等待状态栏记录重置时间）` when `read_week_resets_at_ms()` is None. Choosing a range sets the timeline granularity to `RANGE_BUCKETS[range]` (custom keeps it). Every change calls `save_state`.
  - Key `c`: dialog to set `cycle_start_day` (1–28), saved to state.
  - Share table columns: `名称 · 占比 · % · 花费 · 请求 · tokens`; by model add `输入/输出 $/M` and `缓存读/写 $/M` (write = 1 h rate); share bar 18 cells `█`/`░` in the group colour; rows merged into 其他 in the timeline show here individually in grey; empty range shows `无数据`.
  - Startup: `load_cached` then refresh in a worker; timer every `refresh_s`; skip a tick while a refresh is running; after each refresh `write_summary`.

- [ ] **Step 1: Write failing pilot tests** (`asyncio.run` + `app.run_test(size=...)`; a fake `fetch` returning fixed rows; XDG dirs in `tmp_path`)

```python
def test_header_and_share_table():
    app = UsageApp(DEVS, config_dir="x", fetch=fake_fetch)
    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            text = screen_text(app)
            assert "本周期 $" in text and "2/2 台在线" in text
            assert "nasa718" in text and "career-ops" in text
    asyncio.run(go())
def test_offline_line(): ...          # fetch raising for "b" → "b 离线" and its hint on screen, numbers still shown
def test_empty_range_shows_no_data(): ...
def test_narrow_terminal(): ...       # size=(70, 20): mounts, no exception, then resize to (140, 50)
def test_choice_persists(): ...       # change share 分组 to 按 skill via SentenceBar.Changed → state file has share.by == "skill"
def test_once_cli(capsys): ...        # main(["--once", ...]) prints "本周期" line and writes summary.json
```

- [ ] **Step 2: Run** `uv run --with pytest --with textual==<pinned> pytest plugins/billing-usage/tests/test_tui.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement the app shell, `SentenceBar`, `ShareTable`.** The timeline area is a placeholder `Static` until Task 6.
- [ ] **Step 4: Run tests** — Expected: PASS. Then run it for real: `uv run --script plugins/billing-usage/scripts/billing_usage_tui.py`, check the share table looks like the spec mockup in Ghostty.
- [ ] **Step 5: Commit** `git commit -m "feat(billing-usage): terminal UI shell with sentence pickers and share table"`

---

### Task 6: Timeline chart, legend, bucket detail, zoom and isolate

**Files:**
- Modify: `plugins/billing-usage/scripts/tui_widgets.py`, `plugins/billing-usage/scripts/billing_usage_tui.py`
- Create: `plugins/billing-usage/tests/test_render.py`

**Interfaces:**
- Consumes: `usage_time.bucket_start`, `zoom_granularity`, `MIN_SPAN_MS`; `usage_groups.segments`, `fmt_*`
- Produces:
  - `render_bars(stacks: list[list[tuple[float, str]]], height: int, vmax: float) -> list[list[tuple[str, str|None, str|None]]]` — pure; one column per stack (bottom-up `(value, colour)` pairs); returns `height` rows of `(char, fg, bg)` cells, top row first
  - `TimelineChart` widget: `update(rows, by, metric, picked_ms, range_from, range_to)`; reactive window `(start_ms, end_ms) | None`; `only: str | None`
  - `Legend` widget under the chart.

- [ ] **Step 1: Write failing tests**

```python
def test_render_bars_eighths():
    # one column, height 2 rows = 16 eighths, vmax 16: red 3/16, blue 6/16
    cells = render_bars([[(3, "R"), (6, "B")]], height=2, vmax=16)
    bottom, top = cells[1][0], cells[0][0]
    # bottom cell: red 3/8 under blue → ("▃", "R", "B"); top cell: blue 1/8 → ("▁", "B", None)
    assert bottom == ("▃", "R", "B") and top == ("▁", "B", None)
def test_render_bars_empty_and_zero(): ...   # vmax 0 → all blank cells, no exception
```

Cell algorithm (not determined by the signature): walk each column bottom-up in eighths; a cell whose eighths belong to one segment is `█` in that colour; a cell containing one boundary is the lower-block glyph for the lower segment's eighths, `fg` = lower colour, `bg` = upper colour (or `None` above the stack's top). When a cell would contain more than one boundary, give the boundary's eighths to the segment holding the most of that cell. Round values to eighths so stack heights are monotonic.

Pilot tests (`test_tui.py`): bars appear for a fixture day; `+` narrows the window and the granularity word in the sentence shows the finer bucket; `←` after zoom moves the window; selecting a legend entry and pressing `i` leaves only that segment's colour in the chart and the centred total equals that segment's total; switching 分组 clears `only`; moving the highlight to a bar shows the detail lines `名称 值 占比` largest first and a `合计` row without a share.

- [ ] **Step 2: Run** `uv run --with pytest --with textual==<pinned> pytest plugins/billing-usage/tests/test_render.py plugins/billing-usage/tests/test_tui.py -q` — Expected: new tests FAIL.
- [ ] **Step 3: Implement `render_bars`, `TimelineChart`, `Legend`**

Behaviour (spec 时间线的交互细节): axis from the first bucket with usage to the end of the bucket containing the range end; x labels as dates (`10/16`) for day-or-longer views, `HH:MM` otherwise; y-axis max label in the metric's format; total centred above the bars. Keys: `+`/`-` and the mouse wheel zoom around the highlighted bar (window never narrower than `MIN_SPAN_MS`, never wider than the axis); `←`/`→` pan by a quarter of the window when zoomed, otherwise move the bar highlight; mouse hover also highlights. Legend: one entry per segment ≥ 0.1%, `■ <label> <share>`, wraps; highlighted entry dims other segments (blend their colour 60% towards the panel background); `i` toggles `only`. When zoom changes the granularity, rebucket from the already-fetched rows (no refetch needed: rows are per request).

- [ ] **Step 4: Run tests** — Expected: PASS. Run the app in Ghostty and compare with the spec mockup; check no glyph overlaps.
- [ ] **Step 5: Commit** `git commit -m "feat(billing-usage): stacked timeline with legend, bucket detail, zoom and isolate"`

---

### Task 7: Status-line segment

**Files:**
- Modify: `~/.claude/scripts/statusline.sh` (not in the repo; first `cp ~/.claude/scripts/statusline.sh ~/.claude/scripts/statusline.sh.bak-2026-10-22`)

**Interfaces:**
- Consumes: `summary.json` and `limits.json` shapes from Task 4.

- [ ] **Step 1: Record the weekly reset**

After `WEEK_RESET` is read: when non-empty, compare with `limits.json`'s `seven_day_resets_at` and, only if different, write `{"seven_day_resets_at": <value>}` via a temp file + `mv` into `${XDG_CACHE_HOME:-$HOME/.cache}/billing-usage/` (create the dir).

- [ ] **Step 2: Show the segment on row 1**

After `${COST_FMT}`: if `summary.json` exists, read `today_text`, `cycle_text`, `updated_at` with one `jq -r` call; print ` | 今日 <today_text> · 周期 <cycle_text>` in yellow, or dimmed with ` 旧` appended when `now − updated_at > 600`. Missing or unreadable file → print nothing extra.

- [ ] **Step 3: Verify**

Run with a sample payload, three cases (fresh summary, 20-minute-old summary, no summary):
`echo '{"model":{"display_name":"Opus"},"workspace":{"current_dir":"/tmp"},"rate_limits":{"seven_day":{"used_percentage":40,"resets_at":1793095200}}}' | XDG_CACHE_HOME=$TMPDIR/bu bash ~/.claude/scripts/statusline.sh`
Expected: segment present / dimmed with 旧 / absent; `limits.json` written once and unchanged on a second run (`stat` mtime). Timing: `time` of 20 runs with vs without the new block differs by < 50 ms per run.

- [ ] **Step 4: No commit** (file is outside the repo). Note the backup path in the final report.

---

### Task 8: README and acceptance against the real devices

**Files:**
- Modify: `README.md` (billing-usage section)

- [ ] **Step 1: README** — add: start with `uv run --script ~/.claude/plugins/marketplaces/aps/plugins/billing-usage/scripts/billing_usage_tui.py` (suggested alias `bu`), keys (`r` `q` `c` `+` `-` `←` `→` `i` Tab), that the TUI and `/billing-usage:report` share one calculation, cache/state paths, and that the status line segment reads `summary.json`.

- [ ] **Step 2: Acceptance run on the real devices file**

Run: `uv run --script plugins/billing-usage/scripts/billing_usage_tui.py --once` and immediately `python3 plugins/billing-usage/scripts/billing_usage.py --start 2026-10-08`.
Expected: the cycle cost matches the report TOTAL `cost_usd` to the cent and the device count matches; any gap is explained only by requests made between the two runs (rerun if so). An `UNREACHABLE` device in the report is offline in `--once` with the same hint.

- [ ] **Step 3: Offline check** — temporarily point one device at a wrong address in a copy of the devices file (`--devices <copy>`): the TUI keeps running, marks it offline with a fix hint, other numbers unchanged.

- [ ] **Step 4: Full test suite** — `uv run --with pytest --with textual==<pinned> pytest plugins/billing-usage/tests -q` → all PASS.

- [ ] **Step 5: Commit** `git commit -m "docs(billing-usage): how to run the terminal UI"`
