#!/usr/bin/env python3
"""Sum Claude Code token usage and list-price API cost per UTC day from local transcripts.

Reads <config dir>/projects/**/*.jsonl (subagent transcripts included). A request is
written to the transcript several times while it streams, so rows are deduplicated by
(message.id, requestId); the row with the largest output_tokens wins and the earliest
timestamp is the request's day. Scope: this machine and this config dir only.
"""
import argparse
import collections
import glob
import json
import os
from datetime import datetime, timedelta, timezone

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

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
    out = collections.defaultdict(lambda: dict.fromkeys(("requests", "cost") + FIELDS, 0))
    for ts, usage, model in rows:
        bucket = out[f"{ts:%Y-%m-%d}"]
        bucket["requests"] += 1
        for k in FIELDS:
            bucket[k] += usage.get(k) or 0
        bucket["cost"] += cost_of(model, usage) or 0
    return dict(sorted(out.items()))


def print_table(buckets):
    print(f"{'day (UTC)':<14}{'requests':>9}{'input':>12}{'output':>12}{'cache_write':>14}{'cache_read':>15}"
          f"{'in+out':>12}{'all':>16}{'cost_usd':>12}")
    total = dict.fromkeys(("requests", "cost") + FIELDS, 0)
    for name, b in buckets.items():
        for k in total:
            total[k] += b[k]
        _print_row(name, b)
    _print_row("TOTAL", total)


def _print_row(name, b):
    io = b["input_tokens"] + b["output_tokens"]
    everything = io + b["cache_creation_input_tokens"] + b["cache_read_input_tokens"]
    print(f"{name:<14}{b['requests']:>9,}{b['input_tokens']:>12,}{b['output_tokens']:>12,}"
          f"{b['cache_creation_input_tokens']:>14,}{b['cache_read_input_tokens']:>15,}{io:>12,}{everything:>16,}"
          f"{b['cost']:>12,.2f}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", required=True, help="first day of the billing cycle, YYYY-MM-DD (UTC)")
    p.add_argument("--end", help="last day to include, YYYY-MM-DD (UTC); default: now")
    p.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    a = p.parse_args()

    utc = timezone.utc
    start = datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=utc)
    if a.end:
        end = datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=utc) + timedelta(days=1)
    else:
        end = datetime.now(utc)

    rows = collect(os.path.join(a.config_dir, "projects"), start, end)
    print(f"Window (UTC): {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}, {len(rows):,} unique requests")
    print("cost_usd = list-price API equivalent (standard speed, global inference), not what a subscription is billed.\n")
    print_table(tally_by_day(rows))

    unpriced = sorted({r[2] for r in rows if r[2] not in PRICES}, key=str)
    if unpriced:
        print(f"\nWARNING: no price for {', '.join(map(str, unpriced))}; their tokens are counted but cost is 0.")
    premium = sum(1 for r in rows if r[1].get("speed") == "fast" or r[1].get("inference_geo") == "us")
    if premium:
        print(f"\nWARNING: {premium:,} requests used fast mode or US-only inference (cost understated).")


if __name__ == "__main__":
    main()
