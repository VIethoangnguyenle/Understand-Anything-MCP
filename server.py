"""
MCP Server for Understand-Anything Knowledge Graphs.

Multi-project support via PROJECT_ROOTS env var (comma-separated paths).
Loads both knowledge-graph.json and domain-graph.json per project.
All logging goes to stderr (stdout is reserved for MCP stdio protocol).

Env vars:
  PROJECT_ROOTS  — comma-separated project root paths

Run dev:
  PROJECT_ROOTS=/path/to/project mcp dev server.py
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

import kg_loader as kgl

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout = MCP protocol channel)
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[kg-mcp] %(levelname)s %(message)s",
)
log = logging.getLogger("kg-mcp")

# ---------------------------------------------------------------------------
# Multi-project registry with mtime-based cache
# ---------------------------------------------------------------------------

_registry: dict[str, kgl.ProjectGraph] = {}
_mtimes: dict[str, tuple[float, float]] = {}


def _init_registry() -> None:
    """Scan PROJECT_ROOTS and load all graphs."""
    roots_str = os.environ.get("PROJECT_ROOTS", "")
    if not roots_str:
        log.warning("PROJECT_ROOTS not set. No projects loaded.")
        return

    for root in roots_str.split(","):
        root = root.strip()
        if not root:
            continue
        try:
            graph = kgl.load_project(root)
            _registry[graph.name] = graph
            _mtimes[graph.name] = kgl.get_graph_mtimes(root)
            log.info("Loaded project '%s': %d nodes, %d edges, %d domain nodes",
                     graph.name, len(graph.nodes), len(graph.edges), len(graph.domain_nodes))
        except FileNotFoundError as e:
            log.warning("Skipping %s: %s", root, e)
        except ValueError as e:
            log.error("Error loading %s: %s", root, e)


def _resolve_project(project: str | None) -> kgl.ProjectGraph:
    """
    Resolve project by name.
    - If project specified → lookup directly.
    - If None + 1 project → use the only one.
    - If None + N projects → raise error listing available projects.
    """
    if not _registry:
        _init_registry()

    if not _registry:
        raise ValueError(
            "No projects loaded. Set PROJECT_ROOTS env var with paths to projects "
            "that have .understand-anything/knowledge-graph.json"
        )

    if project:
        # Try exact match first, then case-insensitive
        if project in _registry:
            return _check_reload(project)
        for name in _registry:
            if name.lower() == project.lower():
                return _check_reload(name)
        available = ", ".join(sorted(_registry.keys()))
        raise ValueError(f"Project '{project}' not found. Available: {available}")

    if len(_registry) == 1:
        name = next(iter(_registry))
        return _check_reload(name)

    available = ", ".join(sorted(_registry.keys()))
    raise ValueError(
        f"Multiple projects loaded ({len(_registry)}). "
        f"Please specify 'project' parameter. Available: {available}"
    )


def _check_reload(project_name: str) -> kgl.ProjectGraph:
    """Reload graph if files have changed on disk."""
    graph = _registry[project_name]
    current_mtimes = kgl.get_graph_mtimes(graph.root_path)

    if current_mtimes != _mtimes.get(project_name):
        log.info("Reloading project '%s' (files changed)", project_name)
        graph = kgl.load_project(graph.root_path)
        _registry[project_name] = graph
        _mtimes[project_name] = current_mtimes

    return graph


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="knowledge-graph",
    instructions=(
        "Query knowledge graphs from Understand-Anything. "
        "Use list_projects to see available projects. "
        "Use query_nodes to search, get_node_detail for details, "
        "get_relationships for connections, trace_call_chain for call flows, "
        "get_domain_overview/get_domain_detail for business domains."
    ),
)


# ---------------------------------------------------------------------------
# Tool 1: list_projects
# ---------------------------------------------------------------------------

@mcp.tool()
def list_projects() -> str:
    """
    List all registered projects with basic statistics.

    No parameters required. Call this first to see available projects.

    Returns:
        List of projects with node/edge counts and domain info.
    """
    if not _registry:
        _init_registry()

    if not _registry:
        return "No projects loaded. Set PROJECT_ROOTS env var."

    lines = [f"=== {len(_registry)} PROJECT(S) REGISTERED ===\n"]
    for name, graph in sorted(_registry.items()):
        type_counts: dict[str, int] = {}
        for n in graph.nodes:
            type_counts[n.type] = type_counts.get(n.type, 0) + 1

        domains = [dn for dn in graph.domain_nodes if dn.type == "domain"]

        lines.append(
            f"■ {name}\n"
            f"  Root:    {graph.root_path}\n"
            f"  Nodes:   {len(graph.nodes)} | Edges: {len(graph.edges)}\n"
            f"  Layers:  {len(graph.layers)} | Tour stops: {len(graph.tour)}\n"
            f"  Domains: {len(domains)} | Domain nodes: {len(graph.domain_nodes)}\n"
            f"  Types:   {', '.join(f'{t}({c})' for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))}\n"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: get_graph_stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_graph_stats(project: str | None = None) -> str:
    """
    Get comprehensive statistics about a project's knowledge graph.

    Args:
        project: Project name. Leave empty if only one project is loaded.

    Returns:
        Statistics: node/edge counts, type distributions, top domains, layers.
        Includes FRESHNESS ANALYSIS: whether the graph is up-to-date with
        the current codebase (uses git diff against the commit when the
        graph was last analyzed).
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    type_count: dict[str, int] = {}
    for n in graph.nodes:
        type_count[n.type] = type_count.get(n.type, 0) + 1

    rel_count: dict[str, int] = {}
    for e in graph.edges:
        rel_count[e.relation] = rel_count.get(e.relation, 0) + 1

    layer_count: dict[str, int] = {}
    for n in graph.nodes:
        if n.layer:
            layer_count[n.layer] = layer_count.get(n.layer, 0) + 1

    lines = [
        f"=== KNOWLEDGE GRAPH: {graph.name} ===\n",
        f"Total nodes:  {len(graph.nodes)}",
        f"Total edges:  {len(graph.edges)}",
        f"Layers:       {len(graph.layers)}",
        f"Tour stops:   {len(graph.tour)}",
        f"Domain nodes: {len(graph.domain_nodes)}",
    ]

    if graph.project_info:
        pi = graph.project_info
        lines.append(f"\nProject info:")
        if pi.get("description"):
            lines.append(f"  Description: {pi['description']}")
        if pi.get("languages"):
            lines.append(f"  Languages:   {', '.join(pi['languages'])}")
        if pi.get("frameworks"):
            lines.append(f"  Frameworks:  {', '.join(pi['frameworks'])}")

    lines.append("\nNode types:")
    for t, c in sorted(type_count.items(), key=lambda x: -x[1]):
        bar = "█" * min(c // 20, 40)
        lines.append(f"  {t:<15} {c:>5}  {bar}")

    lines.append("\nEdge relations:")
    for r, c in sorted(rel_count.items(), key=lambda x: -x[1]):
        lines.append(f"  {r:<20} {c:>6}")

    if layer_count:
        lines.append(f"\nTop layers (by node count):")
        for l, c in sorted(layer_count.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  {l:<40} {c} nodes")

    # --- Freshness Analysis ---
    freshness = kgl.check_freshness(graph)
    status = freshness["status"]
    status_emoji = {"FRESH": "✅", "STALE": "⚠️", "VERY_STALE": "🔴", "UNKNOWN": "❓"}.get(status, "❓")

    lines.append(f"\n{'='*50}")
    lines.append(f"FRESHNESS: {status_emoji} {status}")
    lines.append(f"{'='*50}")

    if freshness["analyzed_at"]:
        lines.append(f"  Analyzed at:          {freshness['analyzed_at']}")
    if freshness["days_since_analysis"] >= 0:
        lines.append(f"  Days since analysis:  {freshness['days_since_analysis']}")
    if freshness["git_commit_hash"]:
        lines.append(f"  Graph commit:         {freshness['git_commit_hash'][:12]}")

    if status == "FRESH":
        lines.append(f"  → Graph is up-to-date with current codebase.")
    elif status == "STALE":
        lines.append(f"  → {freshness['stale_file_count']} code file(s) changed since analysis.")
        lines.append(f"  → Graph is still usable but may miss recent changes.")
    elif status == "VERY_STALE":
        lines.append(f"  → {freshness['stale_file_count']} code file(s) changed since analysis.")
        lines.append(f"  → Consider running /understand to rebuild the graph.")
    elif status == "UNKNOWN":
        lines.append(f"  → Cannot determine freshness (missing git commit or git not available).")

    if freshness["stale_files_sample"]:
        lines.append(f"\n  Changed files (sample, max 20):")
        for sf in freshness["stale_files_sample"]:
            lines.append(f"    • {sf}")
        if freshness["stale_file_count"] > 20:
            lines.append(f"    ... and {freshness['stale_file_count'] - 20} more")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3: query_nodes
# ---------------------------------------------------------------------------

@mcp.tool()
def query_nodes(
    query: str,
    node_type: str | None = None,
    limit: int = 10,
    offset: int = 0,
    project: str | None = None,
) -> str:
    """
    Search for nodes in the knowledge graph by keyword (weighted fuzzy matching).

    Scoring: name (3x weight) > summary (1.5x) > tags (1x). Exact name matches get a bonus.
    Supports pagination via offset/limit.

    Args:
        query:     Search keyword (e.g., "authentication", "login", "PaymentService").
        node_type: Filter by type: "file", "function", "class", "config", "service". Leave empty for all.
        limit:     Max results per page (default 10).
        offset:    Starting offset for pagination (default 0). Use with limit for paging.
        project:   Project name. Leave empty if only one project.

    Returns:
        List of matching nodes with id, type, name, summary, layer, and tags.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    nodes, total = kgl.search_nodes(graph, query, node_type, limit, offset)

    if not nodes:
        suffix = f" (type={node_type})" if node_type else ""
        return f"No nodes found for query='{query}'{suffix} in project '{graph.name}'."

    start = offset + 1
    end = offset + len(nodes)
    lines = [f"Showing {start}-{end} of {total} match(es) for '{query}' in {graph.name}:\n"]
    for n in nodes:
        tags_str = ", ".join(n.tags) if n.tags else "(none)"
        lines.append(
            f"• [{n.type}] {n.name}\n"
            f"  ID: {n.id}\n"
            f"  Layer: {n.layer or 'N/A'} | Complexity: {n.complexity or 'N/A'} | Lines: {n.size_lines or 'N/A'}\n"
            f"  Summary: {n.summary or '(no description)'}\n"
            f"  Tags: {tags_str}\n"
        )

    if end < total:
        lines.append(f"--- Page {offset // limit + 1}. Next page: offset={end}, limit={limit} ---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 4: get_node_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def get_node_detail(node_id: str, project: str | None = None) -> str:
    """
    Get full details of a specific node by its ID.

    Args:
        node_id: Unique node ID (e.g., "upstream:function:src/auth/login.ts::loginUser").
                 Use query_nodes to find IDs.
        project: Project name. Leave empty if only one project.

    Returns:
        Complete node details including path, layer, tags, complexity.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    node = kgl.get_node_by_id(graph, node_id)
    if node is None:
        return f"Node not found: '{node_id}'. Use query_nodes to find the correct ID."

    lines = [
        f"=== {node.type.upper()}: {node.name} ===",
        f"ID:         {node.id}",
        f"Path:       {node.file_path or 'N/A'}",
        f"Layer:      {node.layer or 'N/A'}",
        f"Complexity: {node.complexity or 'N/A'}",
        f"Lines:      {node.size_lines or 'N/A'}",
        f"\nSummary:\n  {node.summary or '(no description)'}",
    ]
    if node.tags:
        lines.append(f"\nTags: {', '.join(node.tags)}")

    # Show immediate relationships summary
    out_rels = kgl.get_neighbors(graph, node_id, "out")
    in_rels = kgl.get_neighbors(graph, node_id, "in")
    if out_rels or in_rels:
        lines.append(f"\nRelationships: {len(out_rels)} outgoing, {len(in_rels)} incoming")
        lines.append("  (use get_relationships for details)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 4b: get_node_source
# ---------------------------------------------------------------------------

@mcp.tool()
def get_node_source(
    node_id: str,
    max_lines: int = 200,
    project: str | None = None,
) -> str:
    """
    Get the actual source code for a knowledge graph node.

    For function/class nodes: extracts just the method or class block with annotations.
    For file/config nodes: returns the whole file content (truncated if too large).
    Includes line numbers for easy reference.

    Args:
        node_id:   Node ID (use query_nodes to find IDs).
        max_lines: Max lines for whole-file reads (default 200). Does not affect function/class extraction.
        project:   Project name.

    Returns:
        Source code with line numbers, file path, and extraction metadata.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    node = kgl.get_node_by_id(graph, node_id)
    if node is None:
        return f"Node not found: '{node_id}'. Use query_nodes to find the correct ID."

    source, start, end, total = kgl.read_node_source(graph, node, max_lines)

    lines = [
        f"=== SOURCE: [{node.type}] {node.name} ===",
        f"File: {node.file_path}",
    ]

    if start > 0 and end > 0:
        lines.append(f"Lines: {start}-{end} of {total}")
    if node.layer:
        lines.append(f"Layer: {node.layer}")

    lang, _strategy = kgl.detect_language(node.file_path)
    lines.append(f"\n```{lang}\n{source}\n```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 5: get_relationships
# ---------------------------------------------------------------------------

@mcp.tool()
def get_relationships(
    node_id: str,
    direction: str = "both",
    relation_filter: str | None = None,
    project: str | None = None,
) -> str:
    """
    Get all nodes connected to a given node, with relationship types.

    Args:
        node_id:         Node ID to inspect.
        direction:       "out" (this node calls/imports others), "in" (others call/import this), "both".
        relation_filter: Filter by relation: "calls", "imports", "contains", "implements", "extends". Leave empty for all.
        project:         Project name.

    Returns:
        List of relationships in format: "source --[relation]--> target".
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    node = kgl.get_node_by_id(graph, node_id)
    if node is None:
        return f"Node not found: '{node_id}'"

    neighbors = kgl.get_neighbors(graph, node_id, direction, relation_filter)

    if not neighbors:
        return f"Node '{node.name}' has no relationships (direction={direction}, filter={relation_filter})."

    # Group by relation type
    by_rel: dict[str, list[tuple[kgl.Edge, kgl.Node]]] = {}
    for edge, neighbor in neighbors:
        by_rel.setdefault(edge.relation, []).append((edge, neighbor))

    lines = [f"Relationships of [{node.type}] {node.name} (direction={direction}):\n"]
    for rel, items in sorted(by_rel.items()):
        lines.append(f"[{rel.upper()}] ({len(items)}):")
        for edge, neighbor in items:
            # Detect resolved edges: neither source nor target is the queried node
            is_resolved = edge.source != node_id and edge.target != node_id
            suffix = " (via file)" if is_resolved else ""
            if edge.source == node_id or is_resolved:
                lines.append(f"  \u2192 {neighbor.name} ({neighbor.type}){suffix}")
            else:
                lines.append(f"  \u2190 {neighbor.name} ({neighbor.type}){suffix}")
            lines.append(f"    ID: {neighbor.id}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 6: trace_call_chain
# ---------------------------------------------------------------------------

@mcp.tool()
def trace_call_chain(
    start_node_id: str,
    max_depth: int = 3,
    project: str | None = None,
) -> str:
    """
    Trace the function call chain starting from a node (BFS on 'calls' edges).

    For class/file nodes: automatically resolves to contained functions
    and traces call chains from them.

    Args:
        start_node_id: ID of the starting node (function, class, or file).
        max_depth:     Max traversal depth (default 3, max 10).
        project:       Project name.

    Returns:
        Call tree as indented text.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    start_node = kgl.get_node_by_id(graph, start_node_id)
    if start_node is None:
        return f"Node not found: '{start_node_id}'"

    max_depth = min(max_depth, 10)
    chain = kgl.trace_calls(graph, start_node_id, max_depth)

    if len(chain) <= 1:
        return f"'{start_node.name}' does not call any other functions (no 'calls' edges)."

    lines = [f"Call chain from [{start_node.type}] {start_node.name}:\n"]
    for depth, nid, name in chain:
        prefix = "  " * depth + ("└─ " if depth > 0 else "▶ ")
        node = kgl.get_node_by_id(graph, nid)
        type_str = f"[{node.type}]" if node else ""
        lines.append(f"{prefix}{type_str} {name}")
        if node and node.summary and depth > 0:
            lines.append(f"{'  ' * (depth + 1)}↳ {node.summary}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 7: get_layer_info
# ---------------------------------------------------------------------------

@mcp.tool()
def get_layer_info(
    layer_name: str | None = None,
    project: str | None = None,
) -> str:
    """
    List architectural layers, or get nodes belonging to a specific layer.

    Args:
        layer_name: Layer name to inspect. Leave empty to list all layers.
        project:    Project name.

    Returns:
        Layer listing or nodes within a specific layer.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    if layer_name is None:
        if not graph.layers:
            return "No layers defined in this project."
        lines = [f"=== {len(graph.layers)} LAYERS in {graph.name} ===\n"]
        for layer in graph.layers:
            lines.append(
                f"• {layer.name} ({len(layer.node_ids)} nodes)\n"
                f"  {layer.description[:120] if layer.description else '(no description)'}\n"
            )
        return "\n".join(lines)

    # Find layer by name (case-insensitive partial match)
    matched = [l for l in graph.layers if layer_name.lower() in l.name.lower()]
    if not matched:
        all_names = [l.name for l in graph.layers]
        return f"Layer '{layer_name}' not found. Available: {', '.join(all_names)}"

    lines = []
    for layer in matched:
        nodes_in_layer = [kgl.get_node_by_id(graph, nid) for nid in layer.node_ids]
        nodes_in_layer = [n for n in nodes_in_layer if n is not None]

        by_type: dict[str, list[kgl.Node]] = {}
        for n in nodes_in_layer:
            by_type.setdefault(n.type, []).append(n)

        lines.append(f"=== LAYER: {layer.name} ({len(nodes_in_layer)} nodes) ===")
        lines.append(f"{layer.description}\n")

        for t, nodes in sorted(by_type.items()):
            lines.append(f"[{t.upper()}] ({len(nodes)}):")
            for n in nodes[:20]:
                lines.append(f"  • {n.name} — {n.summary[:80] if n.summary else ''}")
            if len(nodes) > 20:
                lines.append(f"  ... and {len(nodes) - 20} more")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 8: get_domain_overview
# ---------------------------------------------------------------------------

@mcp.tool()
def get_domain_overview(project: str | None = None) -> str:
    """
    Get summary of all business domains and their flows (from domain-graph.json).

    Args:
        project: Project name.

    Returns:
        List of domains with flow names, entity counts, and summaries.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    domains = [dn for dn in graph.domain_nodes if dn.type == "domain"]

    if not domains:
        return f"No domain graph data available for project '{graph.name}'."

    lines = [f"=== {len(domains)} BUSINESS DOMAINS in {graph.name} ===\n"]
    for domain in domains:
        flows = kgl.get_domain_children(graph, domain.id, "contains_flow")
        flow_names = [f.name for _, f in flows]
        meta = domain.domain_meta

        lines.append(f"■ {domain.name}")
        lines.append(f"  ID: {domain.id}")
        lines.append(f"  {domain.summary}")
        if meta.get("entities"):
            lines.append(f"  Entities: {', '.join(meta['entities'])}")
        if flow_names:
            lines.append(f"  Flows ({len(flow_names)}): {', '.join(flow_names)}")
        lines.append(f"  Tags: {', '.join(domain.tags)}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 9: get_domain_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def get_domain_detail(domain_name: str, project: str | None = None) -> str:
    """
    Get detailed info about a specific business domain: entities, rules, flows, steps.

    Args:
        domain_name: Domain name (e.g., "authentication", "transfer", "payroll").
                     Fuzzy matching supported.
        project:     Project name.

    Returns:
        Full domain details with entities, business rules, flows and their steps.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    # Find domain by name (fuzzy)
    domains = [dn for dn in graph.domain_nodes if dn.type == "domain"]
    matched = None
    for d in domains:
        if domain_name.lower() in d.name.lower() or domain_name.lower() in d.id.lower():
            matched = d
            break

    if not matched:
        # Try fuzzy search
        results = kgl.search_domain_nodes(graph, domain_name)
        domain_results = [r for r in results if r.type == "domain"]
        if domain_results:
            matched = domain_results[0]

    if not matched:
        available = [d.name for d in domains]
        return f"Domain '{domain_name}' not found. Available: {', '.join(available)}"

    meta = matched.domain_meta
    lines = [
        f"=== DOMAIN: {matched.name} ===",
        f"ID: {matched.id}",
        f"Complexity: {matched.complexity}",
        f"\nSummary:\n  {matched.summary}",
    ]

    if meta.get("entities"):
        lines.append(f"\nEntities: {', '.join(meta['entities'])}")
    if meta.get("businessRules"):
        lines.append("\nBusiness Rules:")
        for rule in meta["businessRules"]:
            lines.append(f"  • {rule}")
    if meta.get("crossDomainInteractions"):
        lines.append("\nCross-Domain Interactions:")
        for interaction in meta["crossDomainInteractions"]:
            lines.append(f"  • {interaction}")

    # Get flows
    flows = kgl.get_domain_children(graph, matched.id, kgl.DOMAIN_REL_CONTAINS_FLOW)
    if flows:
        lines.append(f"\n--- FLOWS ({len(flows)}) ---")
        for _, flow in flows:
            lines.append(f"\n▶ {flow.name}")
            lines.append(f"  {flow.summary}")
            # Show entry point info if available
            flow_meta = flow.domain_meta
            if flow_meta.get("entryPoint"):
                entry_type = flow_meta.get("entryType", "unknown")
                lines.append(f"  Entry: {flow_meta['entryPoint']} ({entry_type})")
            steps = kgl.get_domain_children(graph, flow.id, kgl.DOMAIN_REL_FLOW_STEP)
            if steps:
                steps.sort(key=lambda x: x[0].weight)  # sort by edge weight = step order
                for idx, (_, step) in enumerate(steps, 1):
                    lines.append(f"    {idx}. {step.name}")
                    if step.summary:
                        lines.append(f"       {step.summary}")
                    # Cross-reference to code nodes
                    code_nodes = kgl.resolve_domain_to_code(graph, step, limit=1)
                    if code_nodes:
                        cn = code_nodes[0]
                        lines.append(f"       📎 [{cn.type}] {cn.name} ({cn.id})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 10: get_domain_flow_detail
# ---------------------------------------------------------------------------

@mcp.tool()
def get_domain_flow_detail(flow_name: str, project: str | None = None) -> str:
    """
    Deep-dive into a specific business flow: entry point, all steps with ordering,
    and linked code nodes for each step.

    Use this for detailed understanding of a single flow's implementation.
    For an overview of all domains/flows, use get_domain_overview or get_domain_detail.

    Args:
        flow_name: Flow name (e.g., "SAGA Xử lý Lương", "init payroll", "recovery retry").
                   Fuzzy matching supported.
        project:   Project name.

    Returns:
        Full flow details with entry point, ordered steps, step summaries,
        and cross-referenced code nodes for each step.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    # Find flow by name (fuzzy)
    flows = [dn for dn in graph.domain_nodes if dn.type == "flow"]
    matched = None
    for f in flows:
        if flow_name.lower() in f.name.lower() or flow_name.lower() in f.id.lower():
            matched = f
            break

    if not matched:
        results = kgl.search_domain_nodes(graph, flow_name)
        flow_results = [r for r in results if r.type == "flow"]
        if flow_results:
            matched = flow_results[0]

    if not matched:
        available = [f.name for f in flows]
        return f"Flow '{flow_name}' not found. Available flows ({len(available)}):\n" + "\n".join(f"  • {n}" for n in available)

    # Find parent domain
    parent_domain = None
    for edge in graph.domain_edges:
        if edge.target == matched.id and edge.relation == kgl.DOMAIN_REL_CONTAINS_FLOW:
            parent_domain = kgl.get_domain_node_by_id(graph, edge.source)
            break

    lines = [
        f"=== FLOW: {matched.name} ===",
        f"ID: {matched.id}",
        f"Complexity: {matched.complexity}",
    ]
    if parent_domain:
        lines.append(f"Domain: {parent_domain.name} ({parent_domain.id})")
    lines.append(f"\nSummary:\n  {matched.summary}")

    # Entry point
    flow_meta = matched.domain_meta
    if flow_meta.get("entryPoint"):
        entry_type = flow_meta.get("entryType", "unknown")
        lines.append(f"\nEntry Point: {flow_meta['entryPoint']}")
        lines.append(f"Entry Type:  {entry_type}")

    # Steps with full detail
    steps = kgl.get_domain_children(graph, matched.id, kgl.DOMAIN_REL_FLOW_STEP)
    if steps:
        steps.sort(key=lambda x: x[0].weight)
        lines.append(f"\n--- STEPS ({len(steps)}) ---")
        for idx, (_, step) in enumerate(steps, 1):
            lines.append(f"\n  {idx}. {step.name}")
            if step.summary:
                lines.append(f"     {step.summary}")
            if step.tags:
                lines.append(f"     Tags: {', '.join(step.tags)}")

            # Cross-reference to code nodes (show up to 3)
            code_nodes = kgl.resolve_domain_to_code(graph, step, limit=3)
            if code_nodes:
                lines.append(f"     Code references:")
                for cn in code_nodes:
                    lines.append(f"       📎 [{cn.type}] {cn.name}")
                    lines.append(f"         ID: {cn.id}")
                    if cn.layer:
                        lines.append(f"         Layer: {cn.layer}")
    else:
        lines.append("\n(no steps defined for this flow)")

    # Cross-domain interactions from parent domain
    if parent_domain and parent_domain.domain_meta.get("crossDomainInteractions"):
        lines.append("\n--- CROSS-DOMAIN INTERACTIONS ---")
        for interaction in parent_domain.domain_meta["crossDomainInteractions"]:
            lines.append(f"  • {interaction}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 11: find_entry_points
# ---------------------------------------------------------------------------

@mcp.tool()
def find_entry_points(project: str | None = None) -> str:
    """
    Find functions that are NOT called by any other function (potential API endpoints / entry points).

    Args:
        project: Project name.

    Returns:
        List of entry point functions with their layer and summary.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    entries = kgl.find_entry_points(graph)

    if not entries:
        return "No entry points found."

    # Group by layer
    by_layer: dict[str, list[kgl.Node]] = {}
    for n in entries:
        by_layer.setdefault(n.layer or "(no layer)", []).append(n)

    lines = [f"=== {len(entries)} ENTRY POINT(S) in {graph.name} ===\n"]
    for layer, nodes in sorted(by_layer.items()):
        lines.append(f"[{layer}] ({len(nodes)} functions):")
        for n in nodes[:30]:
            lines.append(f"  • {n.name}")
            if n.summary:
                lines.append(f"    {n.summary[:100]}")
            lines.append(f"    ID: {n.id}")
        if len(nodes) > 30:
            lines.append(f"  ... and {len(nodes) - 30} more")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 11: find_impact
# ---------------------------------------------------------------------------

@mcp.tool()
def find_impact(
    node_id: str,
    max_depth: int = 3,
    project: str | None = None,
) -> str:
    """
    Blast radius analysis: find all nodes that would be affected if this node changes.

    Follows incoming imports, calls, extends, and implements edges in reverse.

    Args:
        node_id:   Node ID to analyze impact for.
        max_depth: Max traversal depth (default 3, max 10).
        project:   Project name.

    Returns:
        List of affected nodes grouped by depth level.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    node = kgl.get_node_by_id(graph, node_id)
    if node is None:
        return f"Node not found: '{node_id}'"

    max_depth = min(max_depth, 10)
    impact = kgl.find_impact(graph, node_id, max_depth)

    if len(impact) <= 1:
        return f"No nodes depend on '{node.name}'."

    # Group by depth
    by_depth: dict[int, list[tuple[str, str, str]]] = {}
    for depth, nid, name, rel in impact:
        if depth == 0:
            continue
        by_depth.setdefault(depth, []).append((nid, name, rel))

    total = sum(len(v) for v in by_depth.values())
    lines = [f"=== IMPACT ANALYSIS: {node.name} ({total} affected nodes) ===\n"]

    for depth in sorted(by_depth.keys()):
        items = by_depth[depth]
        lines.append(f"Depth {depth} ({len(items)} nodes):")
        for nid, name, rel in items:
            affected = kgl.get_node_by_id(graph, nid)
            type_str = f"[{affected.type}]" if affected else ""
            lines.append(f"  {type_str} {name} ←[{rel}]")
            lines.append(f"    ID: {nid}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 12: get_tour
# ---------------------------------------------------------------------------

@mcp.tool()
def get_tour(
    stop_index: int | None = None,
    project: str | None = None,
) -> str:
    """
    Get the guided project tour — a curated walkthrough of the most important parts of the codebase.

    Tour stops are ordered sequences that explain the project's key components,
    each linking to specific nodes in the knowledge graph.

    Args:
        stop_index: Specific tour stop number (1-based) to expand with full node details.
                    Leave empty to list all stops.
        project:    Project name.

    Returns:
        Tour overview or detailed stop with linked nodes.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    if not graph.tour:
        return f"No guided tour available for project '{graph.name}'."

    # List all stops
    if stop_index is None:
        lines = [f"=== GUIDED TOUR: {graph.name} ({len(graph.tour)} stops) ===\n"]
        for stop in sorted(graph.tour, key=lambda s: s.order):
            lines.append(
                f"  {stop.order}. {stop.title}\n"
                f"     {stop.description[:120]}\n"
                f"     Nodes: {len(stop.node_ids)}\n"
            )
        lines.append("Use get_tour(stop_index=N) to expand a stop with full node details.")
        return "\n".join(lines)

    # Expand specific stop
    matched = [s for s in graph.tour if s.order == stop_index]
    if not matched:
        return f"Tour stop {stop_index} not found. Valid range: 1-{len(graph.tour)}"

    stop = matched[0]
    lines = [
        f"=== TOUR STOP {stop.order}: {stop.title} ===",
        f"\n{stop.description}\n",
        f"--- LINKED NODES ({len(stop.node_ids)}) ---",
    ]

    for nid in stop.node_ids:
        node = kgl.get_node_by_id(graph, nid)
        if node:
            lines.append(
                f"\n• [{node.type}] {node.name}"
                f"\n  Path: {node.file_path or 'N/A'}"
                f"\n  Layer: {node.layer or 'N/A'}"
                f"\n  {node.summary or '(no description)'}"
                f"\n  ID: {node.id}"
            )
        else:
            lines.append(f"\n• (missing node) {nid}")

    # Navigation
    nav = []
    if stop.order > 1:
        nav.append(f"← Previous: stop_index={stop.order - 1}")
    next_stops = [s for s in graph.tour if s.order == stop.order + 1]
    if next_stops:
        nav.append(f"→ Next: stop_index={stop.order + 1}")
    if nav:
        lines.append(f"\n{' | '.join(nav)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 13: find_path
# ---------------------------------------------------------------------------

@mcp.tool()
def find_path(
    source_id: str,
    target_id: str,
    max_depth: int = 6,
    project: str | None = None,
) -> str:
    """
    Find the shortest path between two nodes in the knowledge graph.

    Uses undirected BFS — traverses both outgoing and incoming edges.
    Useful for understanding how two components are connected.

    Args:
        source_id: Starting node ID.
        target_id: Destination node ID.
        max_depth: Maximum path length (default 6, max 10).
        project:   Project name.

    Returns:
        Path from source to target showing each hop and relation.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    source_node = kgl.get_node_by_id(graph, source_id)
    if source_node is None:
        return f"Source node not found: '{source_id}'"
    target_node = kgl.get_node_by_id(graph, target_id)
    if target_node is None:
        return f"Target node not found: '{target_id}'"

    max_depth = min(max_depth, 10)
    path = kgl.find_shortest_path(graph, source_id, target_id, max_depth)

    if not path:
        return (
            f"No path found between '{source_node.name}' and '{target_node.name}' "
            f"within {max_depth} hops."
        )

    lines = [
        f"=== PATH: {source_node.name} → {target_node.name} ({len(path) - 1} hops) ===\n"
    ]
    for i, (nid, name, rel) in enumerate(path):
        node = kgl.get_node_by_id(graph, nid)
        type_str = f"[{node.type}]" if node else ""
        if i == 0:
            lines.append(f"▶ {type_str} {name}")
        else:
            lines.append(f"  {'  ' * (i - 1)}└─[{rel}]─→ {type_str} {name}")
        lines.append(f"  {'  ' * i}ID: {nid}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 14: get_class_hierarchy
# ---------------------------------------------------------------------------

@mcp.tool()
def get_class_hierarchy(
    class_id: str,
    direction: str = "both",
    max_depth: int = 5,
    project: str | None = None,
) -> str:
    """
    Show the inheritance hierarchy for a class (extends/implements tree).

    Args:
        class_id:  Class node ID to analyze.
        direction: "up" (parents/supertypes), "down" (children/subtypes), "both".
        max_depth: Max traversal depth (default 5, max 10).
        project:   Project name.

    Returns:
        Inheritance tree showing parent and child classes with relation types.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    node = kgl.get_node_by_id(graph, class_id)
    if node is None:
        return f"Node not found: '{class_id}'"

    max_depth = min(max_depth, 10)
    hierarchy = kgl.get_class_hierarchy(graph, class_id, direction, max_depth)

    if len(hierarchy) <= 1:
        return f"'{node.name}' has no extends/implements relationships (direction={direction})."

    parents = [(d, nid, name, rel) for d, nid, name, rel in hierarchy if d < 0]
    children = [(d, nid, name, rel) for d, nid, name, rel in hierarchy if d > 0]

    lines = [f"=== CLASS HIERARCHY: {node.name} ===\n"]

    if parents:
        lines.append(f"SUPERTYPES ({len(parents)}):")
        for depth, nid, name, rel in parents:
            indent = "  " * abs(depth)
            lines.append(f"  {indent}▲ [{rel}] {name}")
            lines.append(f"  {indent}  ID: {nid}")
        lines.append("")

    lines.append(f"● {node.name} (self)")
    lines.append(f"  ID: {class_id}\n")

    if children:
        lines.append(f"SUBTYPES ({len(children)}):")
        for depth, nid, name, rel in children:
            indent = "  " * depth
            lines.append(f"  {indent}▼ [{rel}] {name}")
            lines.append(f"  {indent}  ID: {nid}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 15: search_by_file_path
# ---------------------------------------------------------------------------

@mcp.tool()
def search_by_file_path(
    path_pattern: str,
    node_type: str | None = None,
    limit: int = 30,
    project: str | None = None,
) -> str:
    """
    Find nodes by file path pattern (substring match).

    Useful for finding all nodes in a package, module, or directory.
    Case-insensitive.

    Args:
        path_pattern: Substring to match in file paths (e.g., "payroll", "com/vietbank/sme", "auth").
        node_type:    Optional filter by type: "file", "function", "class". Leave empty for all.
        limit:        Max results (default 30, max 50).
        project:      Project name.

    Returns:
        List of matching nodes sorted by file path.
    """
    try:
        graph = _resolve_project(project)
    except ValueError as e:
        return f"Error: {e}"

    limit = min(limit, 50)
    nodes = kgl.search_by_path(graph, path_pattern, node_type, limit)

    if not nodes:
        suffix = f" (type={node_type})" if node_type else ""
        return f"No nodes found matching path '{path_pattern}'{suffix}."

    # Group by type
    by_type: dict[str, list[kgl.Node]] = {}
    for n in nodes:
        by_type.setdefault(n.type, []).append(n)

    lines = [f"Found {len(nodes)} node(s) matching path '{path_pattern}':\n"]
    for t, items in sorted(by_type.items()):
        lines.append(f"[{t.upper()}] ({len(items)}):")
        for n in items:
            lines.append(f"  • {n.name}")
            lines.append(f"    Path: {n.file_path}")
            lines.append(f"    ID: {n.id}")
        lines.append("")

    if len(nodes) >= limit:
        lines.append(f"--- Showing first {limit} results. Narrow your search for more precision. ---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Starting KG MCP Server...")
    _init_registry()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
