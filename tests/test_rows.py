import calendar
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from billing_usage import _total, fetch_device_rows, fetch_remote, summarize, summarize_rows
from conftest import assistant, write_transcript

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(os.path.join(HERE, "..", "scripts", "billing_usage.py"))
START_1008 = datetime(2026, 10, 8, tzinfo=timezone.utc)
END_1009 = datetime(2026, 10, 9, tzinfo=timezone.utc)
ROW_KEYS = {"t", "model", "effort", "project", "skill", "input", "output",
            "cache_write", "cache_write_1h", "cache_read", "cost", "premium"}


def ms(iso):
    """Epoch milliseconds of a 'YYYY-MM-DDTHH:MM:SSZ' string, computed without the code under test."""
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) * 1000


def run_cli(args):
    """stdout of the script run as a subprocess, as the report skill runs it."""
    proc = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, check=True)
    return proc.stdout


def read_golden(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


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


def test_rows_fields(projects_dir):
    rows = summarize_rows(str(projects_dir.parent), START_1008, END_1009)["rows"]
    assert all(set(r) == ROW_KEYS for r in rows)
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)
    a = next(r for r in rows if r["skill"] == "investigate")
    assert (a["input"], a["cache_write"], a["cache_write_1h"], a["cache_read"]) == (100, 3000, 2000, 50000)
    assert a["premium"] is False
    # 100 in, 1000 write at 5m, 2000 write at 1h, 50000 read, 40 out; claude-opus-5-5 prices.
    assert a["cost"] == pytest.approx((100 * 4 + 1000 * 5 + 2000 * 8 + 50000 * 0.20 + 40 * 20) / 1e6)
    b = next(r for r in rows if r["model"] == "claude-sonnet-5-5")
    assert b["t"] == ms("2026-10-08T12:00:00Z") + 250     # millisecond precision survives
    assert (b["premium"], b["cache_write_1h"]) == (True, 0)   # speed == "fast"
    c = next(r for r in rows if r["model"] == "claude-unknown-9")
    assert (c["premium"], c["project"], c["effort"], c["skill"]) == (True, "unknown", "none", "none")


def test_rows_metadata_comes_from_the_kept_row(tmp_path):
    usage = lambda out: {"input_tokens": 1, "output_tokens": out}
    write_transcript(tmp_path / "projects" / "-x", [
        assistant("2026-10-08T01:00:00Z", "m", "r", "claude-opus-5-5", usage(2), cwd="/early", effort="low"),
        assistant("2026-10-08T01:00:03Z", "m", "r", "claude-opus-5-5", usage(9),
                  cwd="/Late/Dir/", effort="high", attributionSkill="s:k"),
    ])
    (row,) = summarize_rows(str(tmp_path), START_1008, END_1009)["rows"]
    assert (row["output"], row["effort"], row["skill"], row["project"]) == (9, "high", "s:k", "dir")
    assert row["t"] == ms("2026-10-08T01:00:00Z")


def test_rows_sum_equals_report_tally(projects_dir):
    rows = summarize_rows(str(projects_dir.parent), START_1008, END_1009)["rows"]
    report = summarize(str(projects_dir.parent), START_1008, END_1009)
    total = _total(report["days"].values())
    assert sum(r["cost"] or 0 for r in rows) == pytest.approx(total["cost"], abs=1e-9)
    assert sum(r["input"] for r in rows) == total["input_tokens"]
    assert sum(r["output"] for r in rows) == total["output_tokens"]
    assert sum(r["cache_write"] for r in rows) == total["cache_creation_input_tokens"]
    assert sum(r["cache_read"] for r in rows) == total["cache_read_input_tokens"]
    assert len(rows) == total["requests"]
    assert sum(r["premium"] for r in rows) == report["premium"]


def test_report_output_unchanged(projects_dir, local_devices_file):
    # The goldens were captured from the original script on this same fixture, before any edit.
    base = ["--start", "2026-10-08", "--end", "2026-10-08",
            "--config-dir", str(projects_dir.parent), "--devices", local_devices_file]
    assert run_cli(base) == read_golden("golden_report.txt")
    assert run_cli(base + ["--json"]) == read_golden("golden_json.txt")


def test_rows_cli_prints_one_json_line(projects_dir):
    out = run_cli(["--rows", "--start", "2026-10-08", "--end", "2026-10-08", "--config-dir", str(projects_dir.parent)])
    assert len(out.splitlines()) == 1
    expected = summarize_rows(str(projects_dir.parent), START_1008, END_1009)
    assert json.loads(out) == json.loads(json.dumps(expected))


def test_rows_cli_runs_on_python38(projects_dir):
    uv = ["uv", "run", "--python", "3.8", "--no-project", "python"]
    try:
        probe = subprocess.run(uv + ["-c", "import sys; print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"cannot start uv with Python 3.8: {e}")
    if probe.returncode != 0:
        pytest.skip("uv cannot provide Python 3.8 here: " + (probe.stderr.strip().splitlines() or ["?"])[-1])
    assert probe.stdout.strip() == "(3, 8)"
    out = subprocess.run(uv + [SCRIPT, "--rows", "--start", "2026-10-08", "--config-dir", str(projects_dir.parent)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[-1])["rows"]


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """An `ssh` on PATH that skips its options and target, then runs the remote command locally."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text('#!/bin/sh\n'
                   'if [ -n "$FAKE_SSH_FAIL" ]; then echo "$FAKE_SSH_FAIL" >&2; exit 255; fi\n'
                   'while [ "$1" = "-o" ] || [ "$1" = "-p" ] || [ "$1" = "-i" ]; do shift 2; done\n'
                   'shift\n'
                   'echo "banner line"\n'
                   'exec sh -c "$1"\n')
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])


def ssh_device(projects_dir):
    return {"name": "box", "method": "ssh", "address": "box.invalid", "python": sys.executable,
            "config_dir": str(projects_dir.parent)}


def test_fetch_device_rows_over_ssh_matches_local(projects_dir, fake_ssh):
    local = fetch_device_rows({"name": "here", "method": "local"}, "2026-10-08", "2026-10-08", 60,
                              str(projects_dir.parent))
    remote = fetch_device_rows(ssh_device(projects_dir), "2026-10-08", "2026-10-08", 60, "/nonexistent")
    assert len(local["rows"]) == 3 and remote == local
    assert fetch_remote(ssh_device(projects_dir), "--json", "2026-10-08", "2026-10-08", 60)["premium"] == 2


def test_fetch_device_rows_raises_with_the_ssh_error(projects_dir, fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "box: Permission denied (publickey).")
    with pytest.raises(RuntimeError, match="Permission denied"):
        fetch_device_rows(ssh_device(projects_dir), "2026-10-08", None, 60, "/nonexistent")
