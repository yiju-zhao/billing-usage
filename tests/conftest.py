"""Shared fixtures: put scripts/ on sys.path and build transcript sets."""
import json
import os
import pathlib
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))


def write_transcript(directory, rows, name="session.jsonl"):
    """Write `rows` to <directory>/<name>, one per line, and return the path.

    A dict becomes a JSON line; a str is written as is (to plant malformed lines).
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(str(directory), name)
    with open(path, "w") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    return path


def assistant(ts, msg_id, req_id, model, usage, **extra):
    """One assistant transcript row, as Claude Code writes it."""
    return {"type": "assistant", "timestamp": ts, "requestId": req_id, "uuid": f"u-{msg_id}-{ts}",
            "message": {"id": msg_id, "model": model, "usage": usage}, **extra}


def build_projects(base):
    """The reference transcript set under <base>/projects/-x/session.jsonl; returns <base>/projects."""
    opus, sonnet = "claude-opus-5-5", "claude-sonnet-5-5"
    a_cwd = {"cwd": "/Users/u/Repos/NASA718", "effort": "xhigh", "attributionSkill": "investigate"}

    def a_usage(output):
        return {"input_tokens": 100, "output_tokens": output, "cache_creation_input_tokens": 3000,
                "cache_read_input_tokens": 50000,
                "cache_creation": {"ephemeral_1h_input_tokens": 2000, "ephemeral_5m_input_tokens": 1000}}

    rows = [
        # A: one request streamed three times; the last row has the most output.
        assistant("2026-10-08T00:30:00Z", "msg_A", "req_A", opus, a_usage(1), **a_cwd),
        assistant("2026-10-08T00:30:02Z", "msg_A", "req_A", opus, a_usage(5), **a_cwd),
        assistant("2026-10-08T00:30:05Z", "msg_A", "req_A", opus, a_usage(40), **a_cwd),
        # B: sidechain, fast mode, no effort and no skill; the timestamp has milliseconds.
        assistant("2026-10-08T12:00:00.250Z", "msg_B", "req_B", sonnet,
                  {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 500, "speed": "fast"},
                  isSidechain=True, cwd="/a/b/career-ops"),
        # C: a model with no price, US-only inference, no cwd.
        assistant("2026-10-08T18:00:00Z", "msg_C", "req_C", "claude-unknown-9",
                  {"input_tokens": 7, "output_tokens": 9, "cache_creation_input_tokens": 11,
                   "cache_read_input_tokens": 13, "inference_geo": "us"}),
        # Not API requests: a local error notice, a user turn, a half-written line.
        assistant("2026-10-08T19:00:00Z", "msg_S", "req_S", "<synthetic>",
                  {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}),
        {"type": "user", "timestamp": "2026-10-08T19:30:00Z",
         "message": {"role": "user", "content": "hi", "usage": {"input_tokens": 5, "output_tokens": 5}}},
        '{"type": "assistant", "timestamp": "2026-10-08T20:00:00Z", "mess',
        # D: one second before the 2026-10-08 window.
        assistant("2026-10-07T23:59:59Z", "msg_D", "req_D", sonnet,
                  {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 1,
                   "cache_read_input_tokens": 1}, cwd="/a/b/old"),
    ]
    projects = os.path.join(str(base), "projects")
    write_transcript(os.path.join(projects, "-x"), rows)
    return projects


@pytest.fixture
def projects_dir(tmp_path):
    """<tmp>/projects; its parent is the --config-dir to scan."""
    return pathlib.Path(build_projects(tmp_path))


@pytest.fixture
def local_devices_file(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"devices": [{"name": "this-machine", "method": "local"}]}))
    return str(path)
