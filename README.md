# billing-usage

`bu` is a terminal UI for your Claude Code token usage and its API list-price cost, summed across every device you
use. It runs on its own: it reads the transcripts Claude Code writes on each device and never calls a model.

It follows the design of [token-meter](https://github.com/FrankCyh/token-meter). There are two sections, each
headed by a sentence whose words you can change, for example `花费 · 所有设备 · 按模型 · 每 4 小时 · 最近 7 天`:

- a stacked timeline with a legend, a breakdown of the selected bar, zoom, and isolate;
- a share table grouped by model × effort, project, device or skill.

## Install

You need [uv](https://docs.astral.sh/uv/).

```
uv tool install git+https://github.com/yiju-zhao/billing-usage
uv tool upgrade billing-usage      # later
```

## Use

```
bu                         # the terminal UI; refreshes every 15 minutes
bu --once                  # refresh, print one line (本周期 … · 今日 … · n/m 台在线), exit
bu report --start 2026-10-08 [--end 2026-10-31]   # per-day table (UTC) and TOTAL, as plain text
```

`bu` and `bu --once` take these options:

- `--devices FILE`
- `--config-dir DIR`
- `--timeout SECONDS`: how long to wait for each ssh device. Defaults to 120.

`bu report` takes the same options, plus `--json` and `--rows`.

| Key | Action |
| --- | --- |
| `r` | refresh now (it also refreshes every 15 minutes, and never starts a second refresh while one is running) |
| `q` | quit |
| `c` | set the billing-cycle start day, 1 to 28 (default 8; cycles start at 00:00 UTC) |
| `+` (or `=`), `-` | zoom the timeline in and out; the bucket size follows the zoom |
| `←` `→` | move the highlighted bar; once zoomed, pan the window |
| `Shift` + `←` `→` | always move the highlighted bar |
| `↑` `↓` | pick a legend entry |
| `i` | show only the picked entry; `Esc` lets go |
| `Tab`, `Enter`, or click | change a word in a sentence: metric, device, grouping, bucket size, range |

## Devices

List your devices in `~/.config/billing-usage/devices.json`, or pass `--devices FILE`. Each device has a name, a
connection method and, for `ssh`, an address. With no file, `bu` covers this machine only.

```json
{"devices": [
  {"name": "this-machine", "method": "local"},
  {"name": "dev-box", "method": "ssh", "address": "100.64.0.10", "user": "alice"}
]}
```

[`devices.example.json`](devices.example.json) lists every field: `ssh` also takes `port`, `identity_file`,
`python` and `config_dir`. List every device you want counted, this machine included. The `/billing-usage:setup`
skill in the [APS](https://github.com/yiju-zhao/APS) Claude Code marketplace can build this file with you, and
tests each connection as it goes.

- **What is read.** On each device `bu` reads `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`).
  It does not cover claude.ai, devices you did not list, or sessions already removed by `cleanupPeriodDays`.
- **How ssh devices are read.** `bu` pipes `billing_usage.py` (standard library only, Python 3.8+) to the device
  over standard input. The device sends back one line of JSON with one row per request: time, model, effort,
  project folder name, skill, token counts and cost. Nothing is installed or written on the device, and no
  conversation content leaves it.
- **ssh access.** `ssh` runs with `BatchMode=yes`, so each device needs key-based or Tailscale SSH that works
  without a prompt, and `python3`. Trust each device's host key once with `ssh <address>`.
- **Offline devices.** A device that cannot be read shows as `<name> 离线 · 最后成功于 <time> · <fix hint>`.
  `bu` keeps showing that device's last rows. `bu report` marks it `UNREACHABLE` and leaves it out of the totals.
- **Duplicate requests.** A request is written to the transcript several times while it streams, so rows are
  deduplicated by `(message.id, requestId)`, per device. If you sync `~/.claude/projects` between devices, the
  same request can be counted on two of them.

## Numbers

- **One calculation.** `bu` and `bu report` share the code that reads, deduplicates and prices requests, so the
  cycle total in `bu` equals `bu report`'s `TOTAL` for the same devices at the same moment.
- **What cost means.** `cost` is what the same tokens would cost at the public API list price. It is not what a
  subscription bills you.
- **Prices.** The price table is `PRICES` in `src/billing_usage.py`. A model missing from it is counted at $0,
  with a warning. Models listed in `NO_PRICE_NEEDED` in `src/billing_usage_tui.py` get no warning.
- **Transcript format.** Claude Code does not document its transcript format. After a Claude Code upgrade, compare
  the request count with `claude -p "/usage"`.

## Files and status line

- **Cache.** `${XDG_CACHE_HOME:-~/.cache}/billing-usage/` holds:
  - `rows-<device>.json`: the last rows read from each device;
  - `summary.json`: today's and the cycle's cost, and `updated_at`;
  - `limits.json`: `seven_day_resets_at`.
- **State.** Your sentence choices and the cycle day are kept in `${XDG_STATE_HOME:-~/.local/state}/billing-usage/tui.json`.
- **Status line.** A Claude Code status line can show `今日 … · 周期 …` from `summary.json` with one `jq` call, and
  dim it once it is more than 20 minutes old. It can also record Claude Code's `rate_limits.seven_day.resets_at`
  into `limits.json`. Only a status line can record that value, and the range "自每周重置以来" stays unavailable
  until it is there.

## Develop

```
uv run --with pytest pytest -q
uv run bu
```

The design and implementation plan are in [`docs/`](docs/).
