"""Tests for metrics wiring into the FastMCP tool surface.

Two of these are load-bearing for production safety:
  - a failing tool must still re-raise, unchanged
  - a failing metrics writer must not surface to the caller
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics
import server

EXPECTED_TOOLS = {
    "list_projects",
    "get_graph_stats",
    "get_graph_metadata",
    "query_nodes",
    "get_node_detail",
    "get_node_source",
    "get_relationships",
    "trace_call_chain",
    "get_layer_info",
    "get_domain_overview",
    "get_domain_detail",
    "get_domain_flow_detail",
    "find_entry_points",
    "find_impact",
    "get_tour",
    "find_path",
    "get_class_hierarchy",
    "search_by_file_path",
}


@pytest.fixture(autouse=True)
def _isolated_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _sample(query: str, limit: int = 10, project: str | None = None) -> str:
    """Stand-in with the same shape as a real tool."""
    return f"result for {query}"


def test_wrapped_tool_returns_identical_value() -> None:
    wrapped = metrics._wrap(_sample)

    assert wrapped("loan") == _sample("loan")


def test_wrapped_tool_preserves_signature_and_doc() -> None:
    wrapped = metrics._wrap(_sample)

    assert inspect.signature(wrapped) == inspect.signature(_sample)
    assert wrapped.__doc__ == _sample.__doc__
    assert wrapped.__name__ == _sample.__name__


def test_successful_call_logs_outcome_ok(tmp_path: Path) -> None:
    metrics._wrap(_sample)("loan")

    rows = _lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["tool"] == "_sample"
    assert rows[0]["ms"] >= 0


def test_in_band_error_return_is_not_ok(tmp_path: Path) -> None:
    """server.py swallows exceptions and returns "Error: ..." in 16 places.

    If those land as outcome=ok, the error rate reads 0% forever.
    """

    def boom(project: str | None = None) -> str:
        return "Error: something went wrong"

    metrics._wrap(boom)()

    row = _lines(tmp_path)[0]
    assert row["outcome"] == "error"
    assert row["err"] == "in-band"


def test_project_arg_captured_and_null_when_absent(tmp_path: Path) -> None:
    metrics._wrap(_sample)("loan", project="miniapp-cub-be")
    metrics._wrap(_sample)("loan")

    rows = _lines(tmp_path)
    assert rows[0]["project"] == "miniapp-cub-be"
    assert rows[1]["project"] is None


def test_failed_tool_logs_and_reraises(tmp_path: Path) -> None:
    def explode(project: str | None = None) -> str:
        raise ValueError("boom-secret-detail")

    with pytest.raises(ValueError, match="boom-secret-detail"):
        metrics._wrap(explode)()

    raw = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8")
    row = json.loads(raw)
    assert row["outcome"] == "error"
    assert row["err"] == "ValueError"
    # Exception bodies can quote graph content — never log them.
    assert "boom-secret-detail" not in raw


def test_metrics_failure_never_breaks_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(**_kwargs):
        raise RuntimeError("metrics is broken")

    monkeypatch.setattr(metrics, "record", _explode)

    assert metrics._wrap(_sample)("loan") == "result for loan"


def test_caller_flows_from_contextvar(tmp_path: Path) -> None:
    token = metrics.caller_var.set("abc123def456")
    try:
        metrics._wrap(_sample)("loan")
    finally:
        metrics.caller_var.reset(token)

    metrics._wrap(_sample)("loan")

    rows = _lines(tmp_path)
    assert rows[0]["caller"] == "abc123def456"
    assert rows[1]["caller"] is None


def test_sid_null_when_no_header(tmp_path: Path) -> None:
    """stateless_http=True means no mcp-session-id ever arrives."""
    metrics._wrap(_sample)("loan")

    assert _lines(tmp_path)[0]["sid"] is None


def test_tool_schema_unchanged() -> None:
    """Wrapping must not alter the MCP tool contract."""
    tools = asyncio.run(server.mcp.list_tools())

    assert {t.name for t in tools} == EXPECTED_TOOLS

    by_name = {t.name: t for t in tools}

    query_nodes = by_name["query_nodes"]
    assert set(query_nodes.inputSchema["properties"]) == {
        "query",
        "node_type",
        "limit",
        "offset",
        "project",
    }
    assert query_nodes.inputSchema.get("required") == ["query"]
    assert query_nodes.description
    assert "weighted fuzzy matching" in query_nodes.description

    node_detail = by_name["get_node_detail"]
    assert set(node_detail.inputSchema["properties"]) == {"node_id", "project"}
    assert node_detail.inputSchema.get("required") == ["node_id"]


def test_every_registered_tool_is_instrumented() -> None:
    """A tool added later must be measured without anyone remembering to."""
    for tool in server.mcp._tool_manager.list_tools():
        assert getattr(tool.fn, "__wrapped__", None) is not None, (
            f"tool {tool.name} is not wrapped — was it registered before "
            "metrics.install(mcp)?"
        )
