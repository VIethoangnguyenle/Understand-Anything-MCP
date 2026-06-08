"""
kg_loader.py — Graph loader & query engine for Understand-Anything knowledge graphs.

Handles dual-graph loading:
  - knowledge-graph.json  (code-level: files, functions, classes, edges)
  - domain-graph.json     (business-level: domains, flows, steps)

Schema is mapped from the actual Understand-Anything output format.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Dataclasses — Code Graph
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A node in the code knowledge graph (file, function, class, etc.)."""
    id: str
    type: str           # file | function | class | config | document | service | resource | table | concept
    name: str
    file_path: str      # mapped from "filePath"
    summary: str
    tags: list[str]
    complexity: str     # simple | moderate | complex
    size_lines: int     # mapped from "sizeLines"
    layer: str          # enriched from layers[] after load

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            name=data.get("name", ""),
            file_path=data.get("filePath", ""),
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            complexity=data.get("complexity", ""),
            size_lines=data.get("sizeLines", 0) or 0,
            layer="",  # enriched later
        )


@dataclass
class Edge:
    """An edge in the code knowledge graph."""
    source: str
    target: str
    relation: str       # mapped from "type": imports | calls | contains | implements | extends | triggers | related | configures
    direction: str      # forward

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        return cls(
            source=data.get("source", ""),
            target=data.get("target", ""),
            relation=data.get("type", ""),
            direction=data.get("direction", "forward"),
        )


@dataclass
class LayerInfo:
    """An architectural layer grouping nodes."""
    id: str
    name: str
    description: str
    node_ids: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayerInfo:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            node_ids=data.get("nodeIds", []),
        )


@dataclass
class TourStop:
    """A guided tour stop for project walkthrough."""
    order: int
    title: str
    description: str
    node_ids: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TourStop:
        return cls(
            order=data.get("order", 0),
            title=data.get("title", ""),
            description=data.get("description", ""),
            node_ids=data.get("nodeIds", []),
        )


# ---------------------------------------------------------------------------
# Dataclasses — Domain Graph
# ---------------------------------------------------------------------------

@dataclass
class DomainNode:
    """A node in the domain graph (domain, flow, or step)."""
    id: str
    type: str           # domain | flow | step
    name: str
    summary: str
    tags: list[str]
    complexity: str
    domain_meta: dict[str, Any]   # entities, businessRules, crossDomainInteractions (domain type only)
    file_path: str
    line_range: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DomainNode:
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            name=data.get("name", ""),
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            complexity=data.get("complexity", ""),
            domain_meta=data.get("domainMeta", {}),
            file_path=data.get("filePath", ""),
            line_range=data.get("lineRange", ""),
        )


@dataclass
class DomainEdge:
    """An edge in the domain graph."""
    source: str
    target: str
    relation: str       # contains_flow | has_step | triggers | depends_on
    weight: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DomainEdge:
        return cls(
            source=data.get("source", ""),
            target=data.get("target", ""),
            relation=data.get("type", ""),
            weight=data.get("weight", 1.0),
        )


# ---------------------------------------------------------------------------
# ProjectGraph — Container for all data of one project
# ---------------------------------------------------------------------------

@dataclass
class ProjectGraph:
    """Complete graph data for a single project."""
    name: str
    root_path: str
    project_info: dict[str, Any]

    # Code graph
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    layers: list[LayerInfo] = field(default_factory=list)
    tour: list[TourStop] = field(default_factory=list)

    # Domain graph
    domain_nodes: list[DomainNode] = field(default_factory=list)
    domain_edges: list[DomainEdge] = field(default_factory=list)

    # Metadata from meta.json
    analyzed_at: str = ""                        # ISO timestamp when graph was last analyzed
    git_commit_hash: str = ""                     # commit hash at analysis time
    meta_stats: dict[str, Any] = field(default_factory=dict)  # raw stats from meta.json

    # Internal indexes (built after load)
    _node_index: dict[str, Node] = field(default_factory=dict, repr=False)
    _layer_index: dict[str, str] = field(default_factory=dict, repr=False)
    _domain_node_index: dict[str, DomainNode] = field(default_factory=dict, repr=False)

    def build_indexes(self) -> None:
        """Build lookup indexes for fast queries."""
        self._node_index = {n.id: n for n in self.nodes}
        self._domain_node_index = {dn.id: dn for dn in self.domain_nodes}

        # Build reverse layer index: node_id → layer_name
        self._layer_index = {}
        for layer in self.layers:
            for nid in layer.node_ids:
                self._layer_index[nid] = layer.name

        # Enrich nodes with layer info
        for node in self.nodes:
            node.layer = self._layer_index.get(node.id, "")


# ---------------------------------------------------------------------------
# Loading functions
# ---------------------------------------------------------------------------

def load_project(project_root: str) -> ProjectGraph:
    """
    Load knowledge-graph.json + domain-graph.json from a project root.

    Args:
        project_root: Absolute path to the project directory.

    Returns:
        ProjectGraph with all data loaded and indexes built.

    Raises:
        FileNotFoundError: If knowledge-graph.json doesn't exist.
        ValueError: If JSON is malformed.
    """
    kg_path = os.path.join(project_root, ".understand-anything", "knowledge-graph.json")
    dg_path = os.path.join(project_root, ".understand-anything", "domain-graph.json")

    if not os.path.exists(kg_path):
        raise FileNotFoundError(
            f"Knowledge graph not found: {kg_path}\n"
            "Run Understand-Anything first to generate this file."
        )

    # Load code graph
    with open(kg_path, encoding="utf-8") as f:
        try:
            raw_kg: dict[str, Any] = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse {kg_path}: {e}") from e

    nodes = [Node.from_dict(n) for n in raw_kg.get("nodes", [])]
    edges = [Edge.from_dict(e) for e in raw_kg.get("edges", [])]
    layers = [LayerInfo.from_dict(l) for l in raw_kg.get("layers", [])]
    tour = [TourStop.from_dict(t) for t in raw_kg.get("tour", [])]
    project_info = raw_kg.get("project", {})
    project_name = project_info.get("name", os.path.basename(project_root))

    # Load domain graph (optional — project may not have one)
    domain_nodes: list[DomainNode] = []
    domain_edges: list[DomainEdge] = []

    if os.path.exists(dg_path):
        with open(dg_path, encoding="utf-8") as f:
            try:
                raw_dg: dict[str, Any] = json.load(f)
            except json.JSONDecodeError:
                raw_dg = {}  # graceful fallback

        domain_nodes = [DomainNode.from_dict(n) for n in raw_dg.get("nodes", [])]
        domain_edges = [DomainEdge.from_dict(e) for e in raw_dg.get("edges", [])]

    # Load metadata from meta.json
    meta_path = os.path.join(project_root, ".understand-anything", "meta.json")
    analyzed_at = ""
    git_commit_hash = ""
    meta_stats: dict[str, Any] = {}

    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            try:
                raw_meta: dict[str, Any] = json.load(f)
                analyzed_at = raw_meta.get("analyzedAt", "")
                git_commit_hash = raw_meta.get("gitCommitHash", "")
                meta_stats = raw_meta.get("stats", {})
            except json.JSONDecodeError:
                pass  # graceful fallback — meta is supplementary

    graph = ProjectGraph(
        name=project_name,
        root_path=project_root,
        project_info=project_info,
        nodes=nodes,
        edges=edges,
        layers=layers,
        tour=tour,
        domain_nodes=domain_nodes,
        domain_edges=domain_edges,
        analyzed_at=analyzed_at,
        git_commit_hash=git_commit_hash,
        meta_stats=meta_stats,
    )
    graph.build_indexes()

    return graph


def get_graph_mtimes(project_root: str) -> tuple[float, float]:
    """Get mtimes of both graph files for cache invalidation."""
    kg_path = os.path.join(project_root, ".understand-anything", "knowledge-graph.json")
    dg_path = os.path.join(project_root, ".understand-anything", "domain-graph.json")

    kg_mtime = os.path.getmtime(kg_path) if os.path.exists(kg_path) else 0.0
    dg_mtime = os.path.getmtime(dg_path) if os.path.exists(dg_path) else 0.0

    return (kg_mtime, dg_mtime)


def check_freshness(graph: ProjectGraph) -> dict[str, Any]:
    """
    Check how fresh the knowledge graph is by comparing its gitCommitHash
    against the current HEAD.

    Uses `git diff --name-only <commit>..HEAD` to find files changed since analysis.
    Only counts files matching common code extensions.

    Returns dict with:
      - analyzed_at: ISO timestamp of last analysis
      - git_commit_hash: commit hash at analysis time
      - is_stale: bool — True if code changed since analysis
      - days_since_analysis: int
      - stale_file_count: int — number of code files changed
      - stale_files_sample: list[str] — first 20 changed files
      - status: 'FRESH' | 'STALE' | 'VERY_STALE' | 'UNKNOWN'
    """
    log = logging.getLogger("kg-mcp")
    result: dict[str, Any] = {
        "analyzed_at": graph.analyzed_at,
        "git_commit_hash": graph.git_commit_hash,
        "is_stale": False,
        "days_since_analysis": -1,
        "stale_file_count": 0,
        "stale_files_sample": [],
        "status": "UNKNOWN",
    }

    # Calculate days since analysis
    if graph.analyzed_at:
        try:
            analyzed_dt = datetime.fromisoformat(graph.analyzed_at.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - analyzed_dt).days
            result["days_since_analysis"] = days
        except (ValueError, TypeError):
            pass

    # If no git commit hash, we can't do diff — return UNKNOWN
    if not graph.git_commit_hash:
        result["status"] = "UNKNOWN"
        return result

    # Run git diff to find changed files
    code_extensions = {
        ".java", ".kt", ".py", ".js", ".ts", ".go", ".rs",
        ".xml", ".yaml", ".yml", ".properties", ".json",
        ".sql", ".gradle", ".kts",
    }

    changed_files: list[str] | None = None

    # Strategy 1: git diff <commit>..HEAD (most accurate)
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{graph.git_commit_hash}..HEAD"],
            capture_output=True,
            text=True,
            cwd=graph.root_path,
            timeout=15,
        )
        if proc.returncode == 0:
            changed_files = [f for f in proc.stdout.strip().splitlines() if f.strip()]
            result["diff_method"] = "git_diff_commit"
        else:
            log.info("git diff with commit hash failed (commit may not exist locally), falling back to --since")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Strategy 2: git log --since=<analyzedAt> (fallback when commit is from another repo)
    if changed_files is None and graph.analyzed_at:
        try:
            proc = subprocess.run(
                ["git", "log", "--since", graph.analyzed_at, "--name-only", "--pretty=format:"],
                capture_output=True,
                text=True,
                cwd=graph.root_path,
                timeout=15,
            )
            if proc.returncode == 0:
                # Deduplicate — git log may list same file in multiple commits
                seen: set[str] = set()
                deduped: list[str] = []
                for f in proc.stdout.strip().splitlines():
                    f = f.strip()
                    if f and f not in seen:
                        seen.add(f)
                        deduped.append(f)
                changed_files = deduped
                result["diff_method"] = "git_log_since"
            else:
                log.warning("git log --since failed (rc=%d)", proc.returncode)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if changed_files is None:
        result["status"] = "UNKNOWN"
        return result

    # Filter to code files only
    code_changed = [
        f for f in changed_files
        if os.path.splitext(f)[1].lower() in code_extensions
    ]

    result["stale_file_count"] = len(code_changed)
    result["stale_files_sample"] = code_changed[:20]
    result["is_stale"] = len(code_changed) > 0

    # Determine status
    days = result["days_since_analysis"]
    if len(code_changed) == 0:
        result["status"] = "FRESH"
    elif len(code_changed) <= 10 and days <= 7:
        result["status"] = "STALE"  # minor changes
    else:
        result["status"] = "VERY_STALE"  # significant changes

    return result


# ---------------------------------------------------------------------------
# Query functions — Code Graph
# ---------------------------------------------------------------------------

def get_node_by_id(graph: ProjectGraph, node_id: str) -> Node | None:
    """O(1) lookup of a node by its ID."""
    return graph._node_index.get(node_id)


def search_nodes(
    graph: ProjectGraph,
    query: str,
    node_type: str | None = None,
    limit: int = 10,
    offset: int = 0,
    threshold: int = 50,
) -> tuple[list[Node], int]:
    """
    Weighted fuzzy search nodes by name, summary, and tags.

    Scoring weights: name (3x) > summary (1.5x) > tags (1x).
    Exact name match gets a bonus.
    Uses rapidfuzz if available, falls back to substring matching.

    Args:
        graph: The project graph to search.
        query: Search query string.
        node_type: Optional filter by node type.
        limit: Maximum results to return.
        offset: Starting offset for pagination.
        threshold: Minimum score to include (0-100, default 50).

    Returns:
        Tuple of (matching nodes for the requested page, total matches count).
    """
    q = query.lower()
    candidates = graph.nodes if node_type is None else [
        n for n in graph.nodes if n.type == node_type
    ]

    try:
        from rapidfuzz import fuzz

        def score(node: Node) -> float:
            name_lower = node.name.lower()
            summary_lower = node.summary.lower()
            tags_lower = " ".join(node.tags).lower()

            # Weighted scoring: name 3x, summary 1.5x, tags 1x
            name_score = fuzz.token_set_ratio(q, name_lower) * 3.0
            summary_score = fuzz.partial_ratio(q, summary_lower) * 1.5
            tags_score = fuzz.partial_ratio(q, tags_lower) * 1.0

            # Exact name match bonus
            if q == name_lower or q in name_lower:
                name_score += 100

            # Combine: weighted average normalized to 0-100
            return (name_score + summary_score + tags_score) / 5.5

        scored = [(score(n), n) for n in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        filtered = [(s, n) for s, n in scored if s >= threshold]
        total = len(filtered)
        page = [n for _, n in filtered[offset:offset + limit]]
        return page, total

    except ImportError:
        results = []
        for node in candidates:
            haystack = f"{node.name} {node.summary} {' '.join(node.tags)}".lower()
            if q in haystack:
                results.append(node)
        total = len(results)
        return results[offset:offset + limit], total


def get_neighbors(
    graph: ProjectGraph,
    node_id: str,
    direction: str = "both",
    relation_filter: str | None = None,
) -> list[tuple[Edge, Node]]:
    """
    Get neighboring nodes with their connecting edges.

    Args:
        graph: The project graph.
        node_id: ID of the center node.
        direction: "out" (outgoing), "in" (incoming), "both".
        relation_filter: Optional filter by relation type (e.g., "calls", "imports").
    """
    results: list[tuple[Edge, Node]] = []
    for edge in graph.edges:
        if relation_filter and edge.relation != relation_filter:
            continue
        if direction in ("out", "both") and edge.source == node_id:
            target = get_node_by_id(graph, edge.target)
            if target:
                results.append((edge, target))
        if direction in ("in", "both") and edge.target == node_id:
            source = get_node_by_id(graph, edge.source)
            if source:
                results.append((edge, source))
    return results


def trace_calls(
    graph: ProjectGraph,
    start_id: str,
    max_depth: int = 3,
) -> list[tuple[int, str, str]]:
    """
    BFS traversal following 'calls' edges from start node.

    Returns list of (depth, node_id, node_name).
    """
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(start_id, 0)]
    result: list[tuple[int, str, str]] = []

    while queue:
        current_id, depth = queue.pop(0)
        if current_id in visited or depth > max_depth:
            continue
        visited.add(current_id)

        node = get_node_by_id(graph, current_id)
        label = node.name if node else current_id
        result.append((depth, current_id, label))

        if depth < max_depth:
            for edge in graph.edges:
                if edge.source == current_id and edge.relation == "calls":
                    if edge.target not in visited:
                        queue.append((edge.target, depth + 1))

    return result


def find_entry_points(graph: ProjectGraph) -> list[Node]:
    """Find functions that are not called by any other function (potential entry points)."""
    called_ids = {e.target for e in graph.edges if e.relation == "calls"}
    return [
        n for n in graph.nodes
        if n.type == "function" and n.id not in called_ids
    ]


def find_impact(
    graph: ProjectGraph,
    node_id: str,
    max_depth: int = 3,
) -> list[tuple[int, str, str, str]]:
    """
    Reverse BFS — find all nodes that depend on the given node (blast radius).

    Follows incoming edges: imports, calls, extends, implements.

    Returns list of (depth, node_id, node_name, relation).
    """
    impact_relations = {"imports", "calls", "extends", "implements"}
    visited: set[str] = set()
    queue: list[tuple[str, int, str]] = [(node_id, 0, "self")]
    result: list[tuple[int, str, str, str]] = []

    while queue:
        current_id, depth, rel = queue.pop(0)
        if current_id in visited or depth > max_depth:
            continue
        visited.add(current_id)

        node = get_node_by_id(graph, current_id)
        label = node.name if node else current_id
        result.append((depth, current_id, label, rel))

        if depth < max_depth:
            for edge in graph.edges:
                if (edge.target == current_id
                        and edge.relation in impact_relations
                        and edge.source not in visited):
                    queue.append((edge.source, depth + 1, edge.relation))

    return result


# ---------------------------------------------------------------------------
# Source code extraction
# ---------------------------------------------------------------------------

import re

# Max lines to return for whole-file reads
_MAX_FILE_LINES = 200


def _resolve_file_path(graph: ProjectGraph, node: Node) -> str | None:
    """
    Resolve a node's file_path to an absolute path on disk.

    Resolution order:
      1. project_root / file_path  (downstream files)
      2. UPSTREAM_ROOTS / file_path (upstream files — dvnh-common etc.)

    Upstream nodes (ID starting with 'upstream:') that aren't found in the
    project root are resolved against UPSTREAM_ROOTS env var (comma-separated).

    Returns:
        Absolute path if file exists, None otherwise.
    """
    # Try project root first
    candidate = os.path.join(graph.root_path, node.file_path)
    if os.path.exists(candidate):
        return candidate

    # For upstream nodes, try UPSTREAM_ROOTS
    if node.id.startswith("upstream:"):
        upstream_roots = os.environ.get("UPSTREAM_ROOTS", "")
        for root in upstream_roots.split(","):
            root = root.strip()
            if not root:
                continue
            candidate = os.path.join(root, node.file_path)
            if os.path.exists(candidate):
                return candidate

    return None


def read_node_source(
    graph: ProjectGraph,
    node: Node,
    max_lines: int = _MAX_FILE_LINES,
    context_lines: int = 3,
) -> tuple[str, int, int, int]:
    """
    Read source code for a graph node.

    Strategy:
      - file/config/document → whole file (truncated to max_lines)
      - function → extract the method block from the Java file
      - class → extract the class block
      - other → whole file

    Args:
        graph: The project graph (for root_path).
        node: The node to read source for.
        max_lines: Max lines for whole-file reads.
        context_lines: Lines of context before/after extracted symbol.

    Returns:
        Tuple of (source_code, start_line, end_line, total_file_lines).
        Lines are 1-indexed. source_code may be truncated.
    """
    if not node.file_path:
        return "(no file path for this node)", 0, 0, 0

    abs_path = _resolve_file_path(graph, node)
    if abs_path is None:
        return f"(file not found: {node.file_path})", 0, 0, 0

    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return f"(error reading file: {e})", 0, 0, 0

    total = len(all_lines)

    # For file/config/document → return whole file (truncated)
    if node.type in ("file", "config", "document"):
        if total <= max_lines:
            return "".join(all_lines), 1, total, total
        # Show first portion + truncation notice
        content = "".join(all_lines[:max_lines])
        content += f"\n// ... truncated ({total - max_lines} more lines)\n"
        return content, 1, max_lines, total

    # For function/class → try to extract the symbol block
    if node.type in ("function", "class"):
        extracted = _extract_java_symbol(all_lines, node.name, node.type, context_lines)
        if extracted:
            code, start, end = extracted
            return code, start, end, total

    # Fallback: whole file (truncated)
    if total <= max_lines:
        return "".join(all_lines), 1, total, total
    content = "".join(all_lines[:max_lines])
    content += f"\n// ... truncated ({total - max_lines} more lines)\n"
    return content, 1, max_lines, total


def _extract_java_symbol(
    lines: list[str],
    symbol_name: str,
    symbol_type: str,  # "function" or "class"
    context: int = 3,
) -> tuple[str, int, int] | None:
    """
    Extract a Java method or class block by name using brace-counting.

    Returns (code, start_line_1indexed, end_line_1indexed) or None if not found.
    """
    # Build patterns to find the symbol declaration
    if symbol_type == "function":
        # Match: visibility? modifiers? returnType methodName(
        pattern = re.compile(
            rf'\b{re.escape(symbol_name)}\s*\(',
            re.IGNORECASE,
        )
    else:  # class
        # Match: class/interface/enum ClassName
        pattern = re.compile(
            rf'\b(?:class|interface|enum|record)\s+{re.escape(symbol_name)}\b',
            re.IGNORECASE,
        )

    # Find the declaration line
    decl_line_idx = None
    for i, line in enumerate(lines):
        if pattern.search(line):
            # Skip lines that are just comments or strings
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            decl_line_idx = i
            break

    if decl_line_idx is None:
        return None

    # Walk backwards to include annotations and comments
    start_idx = decl_line_idx
    while start_idx > 0:
        prev = lines[start_idx - 1].strip()
        if prev.startswith("@") or prev.startswith("//") or prev.startswith("*") or prev.startswith("/*") or prev == "":
            start_idx -= 1
        else:
            break

    # Find the opening brace
    brace_line = decl_line_idx
    while brace_line < len(lines):
        if "{" in lines[brace_line]:
            break
        brace_line += 1
    else:
        return None  # No opening brace found

    # Brace-counting to find the closing brace
    depth = 0
    end_idx = brace_line
    for i in range(brace_line, len(lines)):
        line = lines[i]
        # Simple brace counting (ignoring strings/comments for speed)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            end_idx = i
            break
    else:
        end_idx = len(lines) - 1

    # Add context lines
    ctx_start = max(0, start_idx - context)
    ctx_end = min(len(lines) - 1, end_idx + context)

    # Build output with line numbers
    result_lines = []
    for i in range(ctx_start, ctx_end + 1):
        marker = "" if start_idx <= i <= end_idx else "  "
        result_lines.append(f"{i + 1:>5} | {lines[i].rstrip()}")

    return "\n".join(result_lines), ctx_start + 1, ctx_end + 1


# ---------------------------------------------------------------------------
# Query functions — Domain Graph
# ---------------------------------------------------------------------------

def get_domain_node_by_id(graph: ProjectGraph, node_id: str) -> DomainNode | None:
    """O(1) lookup of a domain node by its ID."""
    return graph._domain_node_index.get(node_id)


def get_domain_children(
    graph: ProjectGraph,
    parent_id: str,
    relation_filter: str | None = None,
) -> list[tuple[DomainEdge, DomainNode]]:
    """Get child domain nodes (flows of a domain, steps of a flow)."""
    results: list[tuple[DomainEdge, DomainNode]] = []
    for edge in graph.domain_edges:
        if relation_filter and edge.relation != relation_filter:
            continue
        if edge.source == parent_id:
            child = get_domain_node_by_id(graph, edge.target)
            if child:
                results.append((edge, child))
    return results


def search_domain_nodes(
    graph: ProjectGraph,
    query: str,
) -> list[DomainNode]:
    """Weighted fuzzy search domain nodes by name and summary."""
    q = query.lower()
    try:
        from rapidfuzz import fuzz

        def score(node: DomainNode) -> float:
            name_lower = node.name.lower()
            name_score = fuzz.token_set_ratio(q, name_lower) * 3.0
            summary_score = fuzz.partial_ratio(q, node.summary.lower()) * 1.5
            tags_score = fuzz.partial_ratio(q, " ".join(node.tags).lower()) * 1.0
            if q == name_lower or q in name_lower:
                name_score += 100
            return (name_score + summary_score + tags_score) / 5.5

        scored = [(score(n), n) for n in graph.domain_nodes]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for s, n in scored if s >= 40]

    except ImportError:
        return [
            n for n in graph.domain_nodes
            if q in f"{n.name} {n.summary}".lower()
        ]
