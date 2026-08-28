"""Tests for per-call usage metrics (metrics.py).

The load-bearing guarantee here is that writing metrics can never break a tool
call. Several tests below exist only to pin that down.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Ensure project root is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics

EXPECTED_KEYS = {
    "ts",
    "caller",
    "sid",
    "tool",
    "project",
    "args",
    "arg_hash",
    "ms",
    "outcome",
    "res_bytes",
    "err",
    "v",
}


@pytest.fixture(autouse=True)
def _isolated_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every test at its own file and reset module state."""
    monkeypatch.setenv("UA_MCP_METRICS_FILE", str(tmp_path / "metrics.jsonl"))
    metrics._reset()
    yield
    metrics._reset()


def _lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "metrics.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _record(**overrides) -> None:
    kwargs = {
        "tool": "query_nodes",
        "project": "miniapp-cub-be",
        "args": {"query": "loan", "limit": 10},
        "ms": 7,
        "result": "some result",
        "exc": None,
    }
    kwargs.update(overrides)
    metrics.record(**kwargs)


def test_record_writes_valid_jsonl(tmp_path: Path) -> None:
    _record()

    rows = _lines(tmp_path)
    assert len(rows) == 1

    row = rows[0]
    assert set(row) == EXPECTED_KEYS
    assert row["v"] == metrics.SCHEMA_VERSION
    assert row["tool"] == "query_nodes"
    assert row["project"] == "miniapp-cub-be"
    assert row["outcome"] == "ok"
    assert row["ms"] == 7
    assert row["res_bytes"] == len(b"some result")
    # ts must be machine-readable, not a pretty string.
    assert row["ts"].endswith("Z")
    datetime.fromisoformat(row["ts"])


def test_disabled_by_empty_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UA_MCP_METRICS_FILE", "")
    metrics._reset()

    _record()

    assert not (tmp_path / "metrics.jsonl").exists()


def test_unwritable_path_disables_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "UA_MCP_METRICS_FILE", str(tmp_path / "nope" / "deeper" / "metrics.jsonl")
    )
    metrics._reset()

    _record()  # must not raise

    assert not (tmp_path / "nope").exists()


def test_write_failure_disables_after_5_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record()  # force writer init
    handler = metrics._handler
    assert handler is not None

    calls = {"n": 0}

    def _boom(_record_obj):
        calls["n"] += 1
        raise OSError("disk on fire")

    monkeypatch.setattr(handler, "emit", _boom)

    for _ in range(8):
        _record()  # must never raise

    assert calls["n"] == metrics.MAX_CONSECUTIVE_FAILURES
    assert metrics._disabled is True


def test_caller_key_is_stable_and_discriminating() -> None:
    base = metrics.caller_key("10.0.0.1", "claude-code/1.0")

    assert base == metrics.caller_key("10.0.0.1", "claude-code/1.0")
    assert base != metrics.caller_key("10.0.0.2", "claude-code/1.0")
    assert base != metrics.caller_key("10.0.0.1", "sample-agent/0.9")
    # Missing headers must degrade, not explode.
    assert isinstance(metrics.caller_key(None, None), str)
    assert metrics.caller_key(None, None)


def test_caller_key_hides_raw_ip() -> None:
    key = metrics.caller_key("10.60.28.196", "sample-agent/0.9")

    assert "10.60.28.196" not in key
    assert len(key) == 12


def test_arg_hash_stable_and_discriminating() -> None:
    a = metrics.arg_hash("query_nodes", {"a": 1, "b": 2})
    b = metrics.arg_hash("query_nodes", {"b": 2, "a": 1})
    c = metrics.arg_hash("query_nodes", {"a": 1, "b": 3})
    d = metrics.arg_hash("find_impact", {"a": 1, "b": 2})

    assert a == b, "key order must not change the hash"
    assert a != c, "different values must change the hash"
    assert a != d, "same args under a different tool must not collide"


def test_long_args_truncated_but_hash_intact(tmp_path: Path) -> None:
    long_a = "x" * 10_000
    long_b = "x" * 9_000 + "Z" + "x" * 999

    _record(args={"query": long_a})
    _record(args={"query": long_b})

    rows = _lines(tmp_path)
    logged = rows[0]["args"]["query"]
    assert len(logged) == metrics.MAX_ARG_CHARS + 1
    assert logged.endswith("…")
    # Truncation must not collapse two distinct long queries.
    assert rows[0]["arg_hash"] != rows[1]["arg_hash"]


def test_outcome_classification() -> None:
    cases = [
        ("Error: boom", None, "error", "in-band"),
        ("Node not found: 'x'", None, "notfound", None),
        ("Layer 'x' not found. Available: a, b", None, "notfound", None),
        ("No nodes found for query='zzz' in project 'p'.", None, "empty", None),
        (
            "'f' does not call any other functions (no 'calls' edges).",
            None,
            "empty",
            None,
        ),
        ("No path found between 'a' and 'b'", None, "empty", None),
        ("Node 'x' has no relationships (direction=both).", None, "empty", None),
        ("=== 3 PROJECT(S) REGISTERED ===", None, "ok", None),
    ]

    for result, exc, want_outcome, want_err in cases:
        outcome, err = metrics.classify(result, exc)
        assert (outcome, err) == (want_outcome, want_err), result

    outcome, err = metrics.classify(None, ValueError("boom"))
    assert (outcome, err) == ("error", "ValueError")


def test_outcome_sentinels_still_exist_in_server_source() -> None:
    """Guard against message rewording silently degrading classification."""
    source = (
        Path(__file__).resolve().parent.parent / "server.py"
    ).read_text(encoding="utf-8")

    for marker in (
        'f"Error: {e}"',
        " not found",
        "No nodes found for query=",
        "No entry points found.",
        "No path found between",
        " has no relationships",
        " has no extends/implements",
        "does not call any other functions",
        "No nodes depend on",
        "No guided tour available",
    ):
        assert marker in source, (
            f"sentinel {marker!r} vanished from server.py — "
            "update metrics.classify() or restore the message"
        )


def test_rotation_respects_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "MAX_BYTES", 400)
    monkeypatch.setattr(metrics, "BACKUP_COUNT", 3)
    metrics._reset()

    for i in range(200):
        _record(args={"query": f"q{i}", "pad": "p" * 100})

    files = list(tmp_path.glob("metrics.jsonl*"))
    assert len(files) <= metrics.BACKUP_COUNT + 1
    # The live file must still be readable JSONL.
    assert _lines(tmp_path)


def test_non_serializable_args_do_not_crash(tmp_path: Path) -> None:
    class Weird:
        def __repr__(self) -> str:
            return "<weird object>"

    _record(args={"thing": Weird()})

    rows = _lines(tmp_path)
    assert len(rows) == 1
    assert "weird" in rows[0]["args"]["thing"]
