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


# Domain edge relation constants — single source of truth
DOMAIN_REL_CONTAINS_FLOW = "contains_flow"
DOMAIN_REL_FLOW_STEP = "flow_step"
DOMAIN_REL_CROSS_DOMAIN = "cross_domain"
DOMAIN_REL_TRIGGERS = "triggers"
DOMAIN_REL_DEPENDS_ON = "depends_on"


@dataclass
class DomainEdge:
    """An edge in the domain graph."""
    source: str
    target: str
    relation: str       # DOMAIN_REL_* constants
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
    _edges_by_source: dict[str, list[Edge]] = field(default_factory=dict, repr=False)
    _edges_by_target: dict[str, list[Edge]] = field(default_factory=dict, repr=False)
    _domain_edges_by_source: dict[str, list[DomainEdge]] = field(default_factory=dict, repr=False)
    _nodes_by_path: dict[str, list[Node]] = field(default_factory=dict, repr=False)

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

        # Build edge indexes for O(1) source/target lookup
        self._edges_by_source = {}
        self._edges_by_target = {}
        for edge in self.edges:
            self._edges_by_source.setdefault(edge.source, []).append(edge)
            self._edges_by_target.setdefault(edge.target, []).append(edge)

        # Build domain edge index
        self._domain_edges_by_source = {}
        for edge in self.domain_edges:
            self._domain_edges_by_source.setdefault(edge.source, []).append(edge)

        # Build file_path → nodes index for cross-referencing domain↔code
        self._nodes_by_path = {}
        for node in self.nodes:
            if node.file_path:
                self._nodes_by_path.setdefault(node.file_path, []).append(node)


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


def _resolve_to_parent_file(graph: ProjectGraph, node_id: str) -> str | None:
    """Find the file node that contains a class or function node via 'contains' edge."""
    for edge in graph._edges_by_target.get(node_id, []):
        if edge.relation == "contains":
            source = get_node_by_id(graph, edge.source)
            if source and source.type == "file":
                return edge.source
    return None


def _get_contained_function_ids(graph: ProjectGraph, node_id: str) -> list[str]:
    """Get IDs of function nodes contained by a file or class node.

    For class nodes: resolves to parent file first, then finds functions.
    """
    node = get_node_by_id(graph, node_id)
    if not node:
        return []

    file_id = node_id
    if node.type == "class":
        parent = _resolve_to_parent_file(graph, node_id)
        if not parent:
            return []
        file_id = parent
    elif node.type != "file":
        return []

    result = []
    for edge in graph._edges_by_source.get(file_id, []):
        if edge.relation == "contains":
            target = get_node_by_id(graph, edge.target)
            if target and target.type == "function":
                result.append(edge.target)
    return result


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

    Includes transparent resolution for class nodes: when querying a class,
    also returns edges from its parent file (imports, contained functions)
    since the KG models these relationships at file level.

    Args:
        graph: The project graph.
        node_id: ID of the center node.
        direction: "out" (outgoing), "in" (incoming), "both".
        relation_filter: Optional filter by relation type.
    """
    results: list[tuple[Edge, Node]] = []
    seen: set[str] = set()

    def _collect_out(src_id: str) -> None:
        for edge in graph._edges_by_source.get(src_id, []):
            if relation_filter and edge.relation != relation_filter:
                continue
            if edge.target in seen:
                continue
            target = get_node_by_id(graph, edge.target)
            if target:
                seen.add(edge.target)
                results.append((edge, target))

    def _collect_in(tgt_id: str) -> None:
        for edge in graph._edges_by_target.get(tgt_id, []):
            if relation_filter and edge.relation != relation_filter:
                continue
            if edge.source in seen:
                continue
            source = get_node_by_id(graph, edge.source)
            if source:
                seen.add(edge.source)
                results.append((edge, source))

    # 1. Direct edges for the node itself
    if direction in ("out", "both"):
        _collect_out(node_id)
    if direction in ("in", "both"):
        _collect_in(node_id)

    # 2. Edge resolution for class and function nodes
    #    KG schema is file-centric: imports and contains edges live on file nodes.
    #    When querying a class or function, inherit parent file's outgoing edges
    #    so users can see imports and sibling nodes.
    node = get_node_by_id(graph, node_id)
    if node and node.type in ("class", "function"):
        parent_file_id = _resolve_to_parent_file(graph, node_id)
        if parent_file_id:
            if direction in ("out", "both"):
                for edge in graph._edges_by_source.get(parent_file_id, []):
                    if edge.target == node_id:
                        continue  # skip file→contains→this_node (self-ref)
                    if relation_filter and edge.relation != relation_filter:
                        continue
                    if edge.target in seen:
                        continue
                    target = get_node_by_id(graph, edge.target)
                    if target:
                        seen.add(edge.target)
                        results.append((edge, target))

    return results


def trace_calls(
    graph: ProjectGraph,
    start_id: str,
    max_depth: int = 3,
) -> list[tuple[int, str, str]]:
    """
    BFS traversal following 'calls' edges from start node.

    Includes resolution for class/file nodes: automatically resolves to
    contained functions and traces call chains from them.

    Returns list of (depth, node_id, node_name).
    """
    visited: set[str] = set()
    result: list[tuple[int, str, str]] = []

    start_node = get_node_by_id(graph, start_id)
    if not start_node:
        return result

    # Resolution: if starting from class or file, resolve to contained functions
    if start_node.type in ("class", "file"):
        func_ids = _get_contained_function_ids(graph, start_id)
        if func_ids:
            result.append((0, start_id, start_node.name))
            visited.add(start_id)
            queue: list[tuple[str, int]] = [(fid, 1) for fid in func_ids]
        else:
            queue = [(start_id, 0)]
    else:
        queue = [(start_id, 0)]

    while queue:
        current_id, depth = queue.pop(0)
        if current_id in visited or depth > max_depth:
            continue
        visited.add(current_id)

        node = get_node_by_id(graph, current_id)
        label = node.name if node else current_id
        result.append((depth, current_id, label))

        if depth < max_depth:
            for edge in graph._edges_by_source.get(current_id, []):
                if edge.relation == "calls" and edge.target not in visited:
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
            for edge in graph._edges_by_target.get(current_id, []):
                if (edge.relation in impact_relations
                        and edge.source not in visited):
                    queue.append((edge.source, depth + 1, edge.relation))

    return result


# ---------------------------------------------------------------------------
# Cross-graph queries
# ---------------------------------------------------------------------------

def find_shortest_path(
    graph: ProjectGraph,
    source_id: str,
    target_id: str,
    max_depth: int = 6,
) -> list[tuple[str, str, str]]:
    """
    BFS to find shortest path between two nodes (undirected traversal).

    Returns list of (node_id, node_name, relation_used) from source to target.
    Empty list if no path found within max_depth.
    """
    if source_id == target_id:
        node = get_node_by_id(graph, source_id)
        return [(source_id, node.name if node else source_id, "self")]

    # BFS with parent tracking
    visited: set[str] = {source_id}
    # queue items: (current_id, depth)
    queue: list[tuple[str, int]] = [(source_id, 0)]
    # parent map: child_id → (parent_id, relation)
    parent: dict[str, tuple[str, str]] = {}

    while queue:
        current_id, depth = queue.pop(0)
        if depth >= max_depth:
            continue

        # Explore outgoing edges
        for edge in graph._edges_by_source.get(current_id, []):
            if edge.target not in visited:
                visited.add(edge.target)
                parent[edge.target] = (current_id, edge.relation)
                if edge.target == target_id:
                    break
                queue.append((edge.target, depth + 1))

        # Explore incoming edges (undirected search)
        for edge in graph._edges_by_target.get(current_id, []):
            if edge.source not in visited:
                visited.add(edge.source)
                parent[edge.source] = (current_id, edge.relation)
                if edge.source == target_id:
                    break
                queue.append((edge.source, depth + 1))

        if target_id in parent:
            break

    # Reconstruct path
    if target_id not in parent:
        return []

    path_ids: list[tuple[str, str]] = []  # (node_id, relation)
    current = target_id
    while current in parent:
        prev_id, rel = parent[current]
        path_ids.append((current, rel))
        current = prev_id
    path_ids.append((source_id, "start"))
    path_ids.reverse()

    result: list[tuple[str, str, str]] = []
    for nid, rel in path_ids:
        node = get_node_by_id(graph, nid)
        result.append((nid, node.name if node else nid, rel))

    return result


def get_class_hierarchy(
    graph: ProjectGraph,
    class_id: str,
    direction: str = "both",
    max_depth: int = 5,
) -> list[tuple[int, str, str, str]]:
    """
    BFS on extends/implements edges to build inheritance tree.

    Args:
        graph: The project graph.
        class_id: Starting class node ID.
        direction: "up" (parents/supertypes), "down" (children/subtypes), "both".
        max_depth: Maximum traversal depth.

    Returns:
        List of (depth, node_id, node_name, relation).
        Depth 0 is the starting class. Negative depths = parents, positive = children.
    """
    hierarchy_relations = {"extends", "implements"}
    result: list[tuple[int, str, str, str]] = []

    start_node = get_node_by_id(graph, class_id)
    if not start_node:
        return result

    result.append((0, class_id, start_node.name, "self"))

    # Upward: follow extends/implements edges where class_id is SOURCE
    # (this class extends/implements → parent)
    if direction in ("up", "both"):
        visited: set[str] = {class_id}
        queue: list[tuple[str, int]] = [(class_id, 1)]
        while queue:
            current_id, depth = queue.pop(0)
            if depth > max_depth:
                continue
            for edge in graph._edges_by_source.get(current_id, []):
                if edge.relation in hierarchy_relations and edge.target not in visited:
                    visited.add(edge.target)
                    node = get_node_by_id(graph, edge.target)
                    if node:
                        result.append((-depth, edge.target, node.name, edge.relation))
                        queue.append((edge.target, depth + 1))

    # Downward: follow extends/implements edges where class_id is TARGET
    # (children extend/implement → this class)
    if direction in ("down", "both"):
        visited_down: set[str] = {class_id}
        queue_down: list[tuple[str, int]] = [(class_id, 1)]
        while queue_down:
            current_id, depth = queue_down.pop(0)
            if depth > max_depth:
                continue
            for edge in graph._edges_by_target.get(current_id, []):
                if edge.relation in hierarchy_relations and edge.source not in visited_down:
                    visited_down.add(edge.source)
                    node = get_node_by_id(graph, edge.source)
                    if node:
                        result.append((depth, edge.source, node.name, edge.relation))
                        queue_down.append((edge.source, depth + 1))

    # Sort: parents (negative depth) first, then self (0), then children (positive)
    result.sort(key=lambda x: x[0])
    return result


def search_by_path(
    graph: ProjectGraph,
    path_pattern: str,
    node_type: str | None = None,
    limit: int = 50,
) -> list[Node]:
    """
    Find nodes whose file_path contains the given pattern.

    Useful for finding all nodes in a package or module.
    Case-insensitive substring match.

    Uses _nodes_by_path index to scan only distinct paths (O(P) where P =
    unique paths) instead of O(N) over all nodes.

    Args:
        graph: The project graph.
        path_pattern: Substring to match in file_path (e.g., "payroll", "com/vietbank/sme").
        node_type: Optional filter by node type.
        limit: Maximum results to return.

    Returns:
        List of matching nodes, sorted by file_path.
    """
    pattern = path_pattern.lower()
    results: list[Node] = []

    for path, nodes in graph._nodes_by_path.items():
        if pattern in path.lower():
            for node in nodes:
                if node_type and node.type != node_type:
                    continue
                results.append(node)
                if len(results) >= limit:
                    results.sort(key=lambda n: n.file_path)
                    return results

    results.sort(key=lambda n: n.file_path)
    return results


# ---------------------------------------------------------------------------
# Source code extraction
# ---------------------------------------------------------------------------

import re

# Max lines to return for whole-file reads
_MAX_FILE_LINES = 200

# File extension → (language name, extraction strategy)
#   "brace"  = Java/Kotlin/TS/JS/Go brace-counting
#   "indent" = Python indentation-based
_LANG_MAP: dict[str, tuple[str, str]] = {
    ".java": ("java", "brace"),
    ".kt": ("kotlin", "brace"),
    ".kts": ("kotlin", "brace"),
    ".ts": ("typescript", "brace"),
    ".tsx": ("typescript", "brace"),
    ".js": ("javascript", "brace"),
    ".jsx": ("javascript", "brace"),
    ".go": ("go", "brace"),
    ".rs": ("rust", "brace"),
    ".cs": ("csharp", "brace"),
    ".py": ("python", "indent"),
    ".yaml": ("yaml", "none"),
    ".yml": ("yaml", "none"),
    ".xml": ("xml", "none"),
    ".json": ("json", "none"),
    ".sql": ("sql", "none"),
    ".properties": ("properties", "none"),
    ".gradle": ("groovy", "brace"),
}


def detect_language(file_path: str) -> tuple[str, str]:
    """Detect language and extraction strategy from file extension.
    
    Returns (language_name, strategy) where strategy is 'brace', 'indent', or 'none'.
    """
    ext = os.path.splitext(file_path)[1].lower() if file_path else ""
    return _LANG_MAP.get(ext, ("", "brace"))  # default to brace-counting


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
        _lang, strategy = detect_language(node.file_path)
        if strategy == "indent":
            extracted = _extract_python_symbol(all_lines, node.name, node.type, context_lines)
        elif strategy == "brace":
            extracted = _extract_brace_symbol(all_lines, node.name, node.type, context_lines)
        else:
            extracted = None
        if extracted:
            code, start, end = extracted
            return code, start, end, total

    # Fallback: whole file (truncated)
    if total <= max_lines:
        return "".join(all_lines), 1, total, total
    content = "".join(all_lines[:max_lines])
    content += f"\n// ... truncated ({total - max_lines} more lines)\n"
    return content, 1, max_lines, total


def _extract_brace_symbol(
    lines: list[str],
    symbol_name: str,
    symbol_type: str,  # "function" or "class"
    context: int = 3,
) -> tuple[str, int, int] | None:
    """
    Extract a symbol block using brace-counting (Java, Kotlin, TS, JS, Go, etc.).

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


def _extract_python_symbol(
    lines: list[str],
    symbol_name: str,
    symbol_type: str,  # "function" or "class"
    context: int = 3,
) -> tuple[str, int, int] | None:
    """
    Extract a Python function or class block using indentation tracking.

    Handles nested same-name symbols by preferring the shallowest (least-indented)
    definition. Uses word boundary to avoid matching substrings (e.g., 'process'
    matching 'process_payment').

    Returns (code, start_line_1indexed, end_line_1indexed) or None if not found.
    """
    keyword = "def" if symbol_type == "function" else "class"
    # Word boundary (\b) prevents partial matches: "validate" won't match "validate_input"
    pattern = re.compile(
        rf'^\s*{keyword}\s+{re.escape(symbol_name)}\b\s*[\(:]',
    )

    # Find ALL matching declarations, pick the shallowest (top-level preferred)
    candidates: list[tuple[int, int]] = []  # (indent_level, line_idx)
    for i, line in enumerate(lines):
        if pattern.match(line):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            candidates.append((indent, i))

    if not candidates:
        return None

    # Prefer shallowest indentation (outermost definition)
    candidates.sort(key=lambda x: x[0])
    decl_line_idx = candidates[0][1]

    # Walk backwards to include decorators and comments
    start_idx = decl_line_idx
    while start_idx > 0:
        prev = lines[start_idx - 1].strip()
        if prev.startswith("@") or prev.startswith("#") or prev == "":
            start_idx -= 1
        else:
            break

    # Determine the indentation level of the declaration
    decl_indent = len(lines[decl_line_idx]) - len(lines[decl_line_idx].lstrip())

    # Find the end of the block by tracking indentation
    end_idx = decl_line_idx
    for i in range(decl_line_idx + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            end_idx = i
            continue

        # If indentation drops to same or less level, block is over
        current_indent = len(line) - len(line.lstrip())
        if current_indent <= decl_indent:
            break

        end_idx = i

    # Add context lines
    ctx_start = max(0, start_idx - context)
    ctx_end = min(len(lines) - 1, end_idx + context)

    # Build output with line numbers
    result_lines = []
    for i in range(ctx_start, ctx_end + 1):
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
    """Get child domain nodes (flows of a domain, steps of a flow). O(degree) via index."""
    results: list[tuple[DomainEdge, DomainNode]] = []
    for edge in graph._domain_edges_by_source.get(parent_id, []):
        if relation_filter and edge.relation != relation_filter:
            continue
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


# Preferred node types for cross-referencing (class > file > function)
_CODE_TYPE_PRIORITY = {"class": 0, "file": 1, "function": 2}

# Boilerplate class name patterns — low relevance for domain step cross-referencing
_BOILERPLATE_SUFFIXES = (
    "Application", "Config", "Configuration", "Subscriber",
    "Listener", "Interceptor", "Filter", "Aspect", "Advice",
    "Test", "Tests", "Mock", "Stub", "Spec",
    "Constants", "Utils", "Util", "Helper",
)


def _score_domain_relevance(node: Node, domain_node: DomainNode) -> float:
    """
    Score how relevant a code node is to a domain step.

    Higher = more relevant. Considers:
      - Name tokens appearing in step summary/tags (strong signal)
      - Boilerplate class name patterns (penalty)
      - Type priority (class > file)

    Args:
        node: Code node candidate.
        domain_node: Domain step/flow being resolved.

    Returns:
        Float score; higher is better.
    """
    score = 0.0
    node_name = node.name
    # Strip file extension for comparison (e.g. "FooService.java" → "FooService")
    base_name = node_name.rsplit(".", 1)[0] if "." in node_name else node_name

    # --- Positive signals ---

    # Break class name into tokens: "TransReqActionSelectorChain" → ["trans", "req", "action", "selector", "chain"]
    import re as _re
    name_tokens = [t.lower() for t in _re.findall(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$)", base_name)]

    # Check against step summary
    summary_lower = domain_node.summary.lower() if domain_node.summary else ""
    for token in name_tokens:
        if len(token) >= 3 and token in summary_lower:
            score += 20.0  # Strong signal: class name token in step summary

    # Check against step name
    step_name_lower = domain_node.name.lower() if domain_node.name else ""
    for token in name_tokens:
        if len(token) >= 3 and token in step_name_lower:
            score += 15.0

    # Check against step tags
    tags_lower = " ".join(domain_node.tags).lower() if domain_node.tags else ""
    for token in name_tokens:
        if len(token) >= 3 and token in tags_lower:
            score += 10.0

    # Full base_name match in summary (e.g. summary mentions "TransReqActionSelectorChain")
    if base_name.lower() in summary_lower:
        score += 50.0

    # --- Negative signals ---

    # Penalize boilerplate patterns
    if any(base_name.endswith(bp) for bp in _BOILERPLATE_SUFFIXES):
        score -= 50.0

    # Penalize root-level package files (direct children of the package path)
    # e.g. "pkg/FooApplication.java" is less relevant than "pkg/handler/confirm/FooExecutor.java"
    rel_path = node.file_path
    if domain_node.file_path:
        prefix = domain_node.file_path.rstrip("/") + "/"
        if rel_path.startswith(prefix):
            sub_path = rel_path[len(prefix):]
            depth = sub_path.count("/")
            # Files in subdirectories are often more specific
            score += depth * 2.0

    # --- Type priority (minor tiebreaker) ---
    score -= _CODE_TYPE_PRIORITY.get(node.type, 99) * 0.1

    return score


def resolve_domain_to_code(
    graph: ProjectGraph,
    domain_node: DomainNode,
    limit: int = 3,
) -> list[Node]:
    """
    Resolve a domain step/flow to the most relevant code nodes.

    Strategy:
      1. Exact file_path match via O(1) _nodes_by_path index → prefer class nodes.
      2. Prefix match (directory/package path) → collect ALL classes in that
         package, then rank by semantic relevance to the domain step
         (name-in-summary match, boilerplate penalty, subdirectory depth).

    Returns at most `limit` code nodes, sorted by relevance.
    """
    if not domain_node.file_path:
        return []

    fp = domain_node.file_path

    # Strategy 1: Exact file path match (O(1) index lookup)
    exact = graph._nodes_by_path.get(fp, [])
    if exact:
        # Prefer class > file > function
        sorted_nodes = sorted(
            exact,
            key=lambda n: _CODE_TYPE_PRIORITY.get(n.type, 99),
        )
        return sorted_nodes[:limit]

    # Strategy 2: Directory/package prefix match with semantic ranking
    # Domain steps sometimes point to a package directory, not a file.
    # Collect ALL matching nodes (no early exit) then rank by relevance.
    prefix = fp.rstrip("/") + "/"
    seen: set[str] = set()
    matches: list[Node] = []
    for path, nodes in graph._nodes_by_path.items():
        if path.startswith(prefix):
            for n in nodes:
                if n.type in ("class", "file") and n.id not in seen:
                    seen.add(n.id)
                    matches.append(n)

    if matches:
        # Rank by semantic relevance to the domain step
        matches.sort(
            key=lambda n: _score_domain_relevance(n, domain_node),
            reverse=True,
        )
        return matches[:limit]

    return []
