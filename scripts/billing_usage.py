#!/usr/bin/env python3
"""Sum Claude Code token usage and list-price API cost per UTC day, across one or more devices.

Each device is summarised from <config dir>/projects/**/*.jsonl (subagent transcripts
included). A request is written to the transcript several times while it streams, so rows
are deduplicated by (message.id, requestId); the row with the largest output_tokens wins and
the earliest timestamp is the request's day. Devices come from a devices file (see
devices.example.json): "local" reads this machine, "ssh" runs this same script on the device
and reads back one line of JSON. Duplicates are removed per device, not across devices.
"""
import argparse
import collections
import concurrent.futures
import glob
import json
import os
import shlex
import socket
import subprocess
from datetime import datetime, timedelta, timezone

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
BUCKET_KEYS = ("requests", "cost") + FIELDS
DEFAULT_DEVICES_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "billing-usage", "devices.json")
SSH_CONNECT_TIMEOUT = 10

# USD per million tokens: (input, cache write 5m, cache write 1h, cache read, output).
# Source: platform.claude.com/docs/en/about-claude/pricing, read 2026-10-19. A model missing
# here is reported as unpriced; its tokens are still counted.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50),
    "claude-opus-5-5": (4, 5, 8, 0.20, 20),
    "claude-opus-5": (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.5, 4, 0.10, 10),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
}
# Haiku 5.5 charges these when a request's whole prompt (input + cache reads + cache writes) exceeds 100K.
HAIKU_LONG_PROMPT = (0.50, 0.625, 1, 0.05, 2.50)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def cost_of(model, u):
    rates = PRICES.get(model)
    if rates is None:
        return None
    read, write = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
    inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    if model == "claude-haiku-5-5" and inp + read + write > 100_000:
        rates = HAIKU_LONG_PROMPT
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    write_1h = cc.get("ephemeral_1h_input_tokens") or 0
    p_in, p_w5, p_w1, p_read, p_out = rates
    return (inp * p_in + (write - write_1h) * p_w5 + write_1h * p_w1 + read * p_read + out * p_out) / 1e6


def collect(projects_dir, start, end):
    requests = {}  # key -> [first_ts, usage, model]
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    msg, ts = d["message"], parse_ts(d["timestamp"])
                    usage = msg["usage"]
                except (ValueError, KeyError, TypeError):
                    continue
                if d.get("type") != "assistant" or not isinstance(usage, dict) or msg.get("model") == "<synthetic>":
                    continue  # <synthetic> rows are local error notices with zero tokens, not API requests
                key = (msg.get("id"), d.get("requestId")) if msg.get("id") else d.get("uuid")
                prev = requests.get(key)
                if prev is None:
                    requests[key] = [ts, usage, msg.get("model")]
                else:
                    prev[0] = min(prev[0], ts)
                    if usage.get("output_tokens", 0) > prev[1].get("output_tokens", 0):
                        prev[1] = usage
    return [r for r in requests.values() if start <= r[0] < end]


def tally_by_day(rows):
    out = collections.defaultdict(lambda: dict.fromkeys(BUCKET_KEYS, 0))
    for ts, usage, model in rows:
        bucket = out[f"{ts:%Y-%m-%d}"]
        bucket["requests"] += 1
        for k in FIELDS:
            bucket[k] += usage.get(k) or 0
        bucket["cost"] += cost_of(model, usage) or 0
    return dict(sorted(out.items()))


def summarize(config_dir, start, end):
    """One device's result: the JSON a remote run prints and a local run uses directly."""
    rows = collect(os.path.join(config_dir, "projects"), start, end)
    return {
        "days": tally_by_day(rows),
        "unpriced": sorted({str(r[2]) for r in rows if r[2] not in PRICES}),
        "premium": sum(1 for r in rows if r[1].get("speed") == "fast" or r[1].get("inference_geo") == "us"),
    }


def load_devices(path):
    """Devices from the file at `path`; this machine alone when the default file is absent."""
    explicit = path is not None
    path = os.path.expanduser(path or DEFAULT_DEVICES_FILE)
    if not os.path.exists(path):
        if explicit:
            raise SystemExit(f"devices file not found: {path}")
        return [{"name": socket.gethostname(), "method": "local"}]
    try:
        with open(path) as f:
            devices = json.load(f)["devices"]
    except (ValueError, KeyError, TypeError) as e:
        raise SystemExit(f'{path}: expected {{"devices": [...]}} ({type(e).__name__}: {e})')
    names = set()
    for i, d in enumerate(devices):
        name, method = d.get("name"), d.get("method")
        if not name or name in names:
            raise SystemExit(f"{path}: device #{i + 1} needs a unique name")
        names.add(name)
        if method not in ("local", "ssh"):
            raise SystemExit(f'{path}: device "{name}" has method {method!r}; use "local" or "ssh"')
        if method == "ssh" and not d.get("address"):
            raise SystemExit(f'{path}: ssh device "{name}" needs an address')
    if not devices:
        raise SystemExit(f"{path}: no devices listed")
    return devices


def run_ssh(dev, a):
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}"]
    if dev.get("port"):
        ssh += ["-p", str(dev["port"])]
    if dev.get("identity_file"):
        ssh += ["-i", os.path.expanduser(dev["identity_file"])]
    target = f"{dev['user']}@{dev['address']}" if dev.get("user") else dev["address"]
    remote = [dev.get("python", "python3"), "-", "--json", "--start", a.start]
    if a.end:
        remote += ["--end", a.end]
    if dev.get("config_dir"):
        remote += ["--config-dir", dev["config_dir"]]
    with open(os.path.abspath(__file__)) as f:
        source = f.read()
    proc = subprocess.run(ssh + [target, " ".join(shlex.quote(x) for x in remote)],
                          input=source, capture_output=True, text=True, timeout=a.timeout)
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"ssh exited with status {proc.returncode}")
    # Login banners can precede the output, so take the last line that looks like our JSON.
    json_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if not json_lines:
        raise RuntimeError("no JSON in the remote output (is python3 installed there?)")
    return json.loads(json_lines[-1])


def run_device(dev, a, start, end):
    """(result, None) on success, (None, reason) when the device could not be read."""
    try:
        if dev["method"] == "local":
            config_dir = os.path.expanduser(dev.get("config_dir") or a.config_dir)
            return summarize(config_dir, start, end), None
        return run_ssh(dev, a), None
    except subprocess.TimeoutExpired:
        return None, f"timed out after {a.timeout}s"
    except Exception as e:  # one unreachable device must not hide the others
        return None, str(e) or type(e).__name__


# (text found in the error, what to do about it); the first match wins.
HINTS = (
    ("Host key verification failed", "run `ssh {target}` yourself once and answer yes to trust the host key"),
    ("Permission denied", "key login was rejected: check `user` and `identity_file`, or install your public key there"),
    ("Connection refused", "no ssh server is listening: turn on sshd there (macOS: System Settings > General > Sharing > Remote Login)"),
    ("Could not resolve hostname", "check `address`"),
    ("No route to host", "the device is offline or not on this network"),
    ("Network is unreachable", "the device is offline or not on this network"),
    ("timed out", "the device may be asleep or offline; retry, or raise --timeout if it is only slow"),
    ("command not found", "python3 is missing there: install it, or set `python` for this device"),
)


def hint_for(dev, error):
    target = f"{dev['user']}@{dev['address']}" if dev.get("user") else dev.get("address", dev["name"])
    for needle, hint in HINTS:
        if needle in error:
            return hint.format(target=target)
    return None


def merge_days(day_maps):
    out = collections.defaultdict(lambda: dict.fromkeys(BUCKET_KEYS, 0))
    for days in day_maps:
        for day, bucket in days.items():
            for k, v in bucket.items():
                out[day][k] += v
    return dict(sorted(out.items()))


def _row(name, b, status=None):
    io = b["input_tokens"] + b["output_tokens"]
    everything = io + b["cache_creation_input_tokens"] + b["cache_read_input_tokens"]
    line = (f"{name:<20}{b['requests']:>9,}{b['input_tokens']:>12,}{b['output_tokens']:>12,}"
            f"{b['cache_creation_input_tokens']:>14,}{b['cache_read_input_tokens']:>15,}{io:>12,}{everything:>16,}"
            f"{b['cost']:>12,.2f}")
    return line + (f"  {status}" if status else "")


def _header(first):
    return (f"{first:<20}{'requests':>9}{'input':>12}{'output':>12}{'cache_write':>14}{'cache_read':>15}"
            f"{'in+out':>12}{'all':>16}{'cost_usd':>12}")


def _total(buckets):
    total = dict.fromkeys(BUCKET_KEYS, 0)
    for b in buckets:
        for k in total:
            total[k] += b[k]
    return total


def print_days(days):
    print(_header("day (UTC)"))
    for day, b in days.items():
        print(_row(day, b))
    print(_row("TOTAL", _total(days.values())))


def print_devices(devices, outcomes):
    print("\n" + _header("device"))
    for dev, (result, error) in zip(devices, outcomes):
        b = _total(result["days"].values()) if result else dict.fromkeys(BUCKET_KEYS, 0)
        print(_row(dev["name"], b, "ok" if result else f"UNREACHABLE: {error}"))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", required=True, help="first day of the billing cycle, YYYY-MM-DD (UTC)")
    p.add_argument("--end", help="last day to include, YYYY-MM-DD (UTC); default: now")
    p.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    p.add_argument("--devices", help=f"devices file (default: {DEFAULT_DEVICES_FILE}, else this machine only)")
    p.add_argument("--timeout", type=int, default=120, help="seconds to wait for each ssh device (default: 120)")
    p.add_argument("--json", action="store_true", help="print this machine's summary as one JSON line (used over ssh)")
    a = p.parse_args()

    utc = timezone.utc
    start = datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=utc)
    if a.end:
        end = datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=utc) + timedelta(days=1)
    else:
        end = datetime.now(utc)

    if a.json:
        print(json.dumps(summarize(os.path.expanduser(a.config_dir), start, end)))
        return

    devices = load_devices(a.devices)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as pool:
        outcomes = list(pool.map(lambda d: run_device(d, a, start, end), devices))
    results = [r for r, _ in outcomes if r]

    days = merge_days(r["days"] for r in results)
    requests = sum(b["requests"] for b in days.values())
    scope = f" across {len(results)} of {len(devices)} devices" if len(devices) > 1 else ""
    print(f"Window (UTC): {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}, {requests:,} unique requests{scope}")
    print("cost_usd = list-price API equivalent (standard speed, global inference), not what a subscription is billed.\n")
    print_days(days)
    if len(devices) > 1:
        print_devices(devices, outcomes)

    failed = [(d, err) for d, (_, err) in zip(devices, outcomes) if err]
    if failed:
        print(f"\nWARNING: totals exclude {len(failed)} unreachable device(s):")
        for d, err in failed:
            hint = hint_for(d, err)
            print(f"  {d['name']}: {err}" + (f"\n    fix: {hint}" if hint else ""))
    unpriced = sorted({m for r in results for m in r["unpriced"]})
    if unpriced:
        print(f"\nWARNING: no price for {', '.join(unpriced)}; their tokens are counted but cost is 0.")
    premium = sum(r["premium"] for r in results)
    if premium:
        print(f"\nWARNING: {premium:,} requests used fast mode or US-only inference (cost understated).")


if __name__ == "__main__":
    main()
