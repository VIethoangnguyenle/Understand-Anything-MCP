"""
Tests for kg_loader.py — core loader, edge resolution, and cross-graph queries.

Uses minimal fixtures from tests/fixtures/ to avoid dependency on real projects.
"""

import json
import os
import sys
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kg_loader as kgl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def graph() -> kgl.ProjectGraph:
    """Load the test knowledge graph + domain graph."""
    kg_path = os.path.join(FIXTURE_DIR, "knowledge-graph.json")
    dg_path = os.path.join(FIXTURE_DIR, "domain-graph.json")

    with open(kg_path, encoding="utf-8") as f:
        raw_kg = json.load(f)
    with open(dg_path, encoding="utf-8") as f:
        raw_dg = json.load(f)

    nodes = [kgl.Node.from_dict(n) for n in raw_kg.get("nodes", [])]
    edges = [kgl.Edge.from_dict(e) for e in raw_kg.get("edges", [])]
    layers = [kgl.LayerInfo.from_dict(l) for l in raw_kg.get("layers", [])]
    tour = [kgl.TourStop.from_dict(t) for t in raw_kg.get("tour", [])]

    domain_nodes = [kgl.DomainNode.from_dict(n) for n in raw_dg.get("nodes", [])]
    domain_edges = [kgl.DomainEdge.from_dict(e) for e in raw_dg.get("edges", [])]

    g = kgl.ProjectGraph(
        name="test-project",
        root_path=FIXTURE_DIR,
        project_info=raw_kg.get("project", {}),
        nodes=nodes,
        edges=edges,
        layers=layers,
        tour=tour,
        domain_nodes=domain_nodes,
        domain_edges=domain_edges,
    )
    g.build_indexes()
    return g


# ---------------------------------------------------------------------------
# Test: Core Loader & Indexes
# ---------------------------------------------------------------------------

class TestCoreLoader:
    def test_node_count(self, graph: kgl.ProjectGraph):
        assert len(graph.nodes) == 10

    def test_edge_count(self, graph: kgl.ProjectGraph):
        assert len(graph.edges) == 11

    def test_node_index_built(self, graph: kgl.ProjectGraph):
        assert len(graph._node_index) == 10

    def test_edge_indexes_built(self, graph: kgl.ProjectGraph):
        assert len(graph._edges_by_source) > 0
        assert len(graph._edges_by_target) > 0

    def test_domain_edge_index_built(self, graph: kgl.ProjectGraph):
        assert len(graph._domain_edges_by_source) > 0

    def test_layer_enrichment(self, graph: kgl.ProjectGraph):
        ps = kgl.get_node_by_id(graph, "class:PaymentService")
        assert ps is not None
        assert ps.layer == "Service Layer"

    def test_get_node_by_id(self, graph: kgl.ProjectGraph):
        node = kgl.get_node_by_id(graph, "func:processPayment")
        assert node is not None
        assert node.name == "processPayment"
        assert node.type == "function"

    def test_get_node_by_id_not_found(self, graph: kgl.ProjectGraph):
        assert kgl.get_node_by_id(graph, "nonexistent") is None


# ---------------------------------------------------------------------------
# Test: Edge Resolution Layer
# ---------------------------------------------------------------------------

class TestEdgeResolution:
    def test_resolve_class_to_parent_file(self, graph: kgl.ProjectGraph):
        parent = kgl._resolve_to_parent_file(graph, "class:PaymentService")
        assert parent == "file:src/main/Service.java"

    def test_resolve_function_to_parent_file(self, graph: kgl.ProjectGraph):
        parent = kgl._resolve_to_parent_file(graph, "func:processPayment")
        assert parent == "file:src/main/Service.java"

    def test_get_contained_functions_from_file(self, graph: kgl.ProjectGraph):
        funcs = kgl._get_contained_function_ids(graph, "file:src/main/Service.java")
        assert "func:processPayment" in funcs
        assert "func:validateInput" in funcs

    def test_get_contained_functions_from_class(self, graph: kgl.ProjectGraph):
        """Class resolves to parent file, then finds functions."""
        funcs = kgl._get_contained_function_ids(graph, "class:PaymentService")
        assert "func:processPayment" in funcs

    def test_class_neighbors_inherit_file_edges(self, graph: kgl.ProjectGraph):
        """Class nodes should inherit imports and contains from parent file."""
        neighbors = kgl.get_neighbors(graph, "class:PaymentService", "out")
        neighbor_ids = {n.id for _, n in neighbors}

        # Direct edges: extends BaseService, implements IPaymentProcessor
        assert "class:BaseService" in neighbor_ids
        assert "class:IPaymentProcessor" in neighbor_ids

        # Inherited from file: imports GatewayClient, contains functions
        assert "file:src/main/GatewayClient.java" in neighbor_ids
        assert "func:processPayment" in neighbor_ids

    def test_function_neighbors_inherit_file_edges(self, graph: kgl.ProjectGraph):
        """Function nodes should inherit parent file's outgoing edges."""
        neighbors = kgl.get_neighbors(graph, "func:processPayment", "out")
        neighbor_ids = {n.id for _, n in neighbors}

        # Direct edges: calls validateInput, callGateway, sendNotification
        assert "func:validateInput" in neighbor_ids
        assert "func:callGateway" in neighbor_ids

        # Inherited from file: imports GatewayClient
        assert "file:src/main/GatewayClient.java" in neighbor_ids

    def test_no_self_reference_in_resolution(self, graph: kgl.ProjectGraph):
        """Resolved edges should not include file→contains→this_node."""
        neighbors = kgl.get_neighbors(graph, "class:PaymentService", "out")
        # Should not see PaymentService itself in its own neighbor list
        neighbor_ids = {n.id for _, n in neighbors}
        assert "class:PaymentService" not in neighbor_ids

    def test_deduplication(self, graph: kgl.ProjectGraph):
        """Same neighbor should not appear twice from different resolution paths."""
        neighbors = kgl.get_neighbors(graph, "class:PaymentService", "both")
        ids = [n.id for _, n in neighbors]
        assert len(ids) == len(set(ids)), f"Duplicate neighbors found: {ids}"


# ---------------------------------------------------------------------------
# Test: Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_exact_name(self, graph: kgl.ProjectGraph):
        results, total = kgl.search_nodes(graph, "PaymentService")
        assert total > 0
        assert results[0].name == "PaymentService"

    def test_search_with_type_filter(self, graph: kgl.ProjectGraph):
        results, total = kgl.search_nodes(graph, "payment", node_type="function")
        for node in results:
            assert node.type == "function"

    def test_search_pagination(self, graph: kgl.ProjectGraph):
        page1, total = kgl.search_nodes(graph, "payment", limit=2, offset=0)
        page2, _ = kgl.search_nodes(graph, "payment", limit=2, offset=2)
        assert len(page1) <= 2
        # Pages should not overlap
        ids1 = {n.id for n in page1}
        ids2 = {n.id for n in page2}
        assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# Test: Trace Calls
# ---------------------------------------------------------------------------

class TestTraceCalls:
    def test_trace_from_function(self, graph: kgl.ProjectGraph):
        chain = kgl.trace_calls(graph, "func:processPayment", max_depth=2)
        names = [name for _, _, name in chain]
        assert "processPayment" in names
        assert "validateInput" in names
        assert "callGateway" in names

    def test_trace_from_class_resolves_to_functions(self, graph: kgl.ProjectGraph):
        """Starting from class should auto-resolve to contained functions."""
        chain = kgl.trace_calls(graph, "class:PaymentService", max_depth=3)
        names = [name for _, _, name in chain]
        # Should include the class itself at depth 0
        assert "PaymentService" in names
        # Should include functions called by contained functions
        assert "validateInput" in names or "callGateway" in names

    def test_trace_respects_max_depth(self, graph: kgl.ProjectGraph):
        chain = kgl.trace_calls(graph, "func:processPayment", max_depth=0)
        assert len(chain) == 1  # just the start node

    def test_trace_nonexistent_node(self, graph: kgl.ProjectGraph):
        chain = kgl.trace_calls(graph, "nonexistent")
        assert chain == []


# ---------------------------------------------------------------------------
# Test: Find Impact (Blast Radius)
# ---------------------------------------------------------------------------

class TestFindImpact:
    def test_impact_of_gateway(self, graph: kgl.ProjectGraph):
        """callGateway is called by processPayment → should appear in blast radius."""
        impact = kgl.find_impact(graph, "func:callGateway", max_depth=3)
        names = [name for _, _, name, _ in impact]
        assert "callGateway" in names  # self
        assert "processPayment" in names  # caller

    def test_impact_of_base_class(self, graph: kgl.ProjectGraph):
        """BaseService is extended by PaymentService → should appear."""
        impact = kgl.find_impact(graph, "class:BaseService", max_depth=3)
        names = [name for _, _, name, _ in impact]
        assert "PaymentService" in names


# ---------------------------------------------------------------------------
# Test: Find Shortest Path (P3.1)
# ---------------------------------------------------------------------------

class TestFindShortestPath:
    def test_path_direct_edge(self, graph: kgl.ProjectGraph):
        path = kgl.find_shortest_path(graph, "func:processPayment", "func:validateInput")
        assert len(path) == 2  # source + target
        assert path[0][0] == "func:processPayment"
        assert path[1][0] == "func:validateInput"

    def test_path_multi_hop(self, graph: kgl.ProjectGraph):
        """PaymentService → extends → BaseService (direct), so path should be short."""
        path = kgl.find_shortest_path(graph, "class:PaymentService", "class:BaseService")
        assert len(path) >= 2

    def test_path_self(self, graph: kgl.ProjectGraph):
        path = kgl.find_shortest_path(graph, "func:processPayment", "func:processPayment")
        assert len(path) == 1

    def test_path_not_found(self, graph: kgl.ProjectGraph):
        """No path to a nonexistent node."""
        path = kgl.find_shortest_path(graph, "func:processPayment", "nonexistent")
        assert path == []


# ---------------------------------------------------------------------------
# Test: Class Hierarchy (P3.2)
# ---------------------------------------------------------------------------

class TestClassHierarchy:
    def test_hierarchy_both_directions(self, graph: kgl.ProjectGraph):
        hierarchy = kgl.get_class_hierarchy(graph, "class:PaymentService", "both")
        names = [name for _, _, name, _ in hierarchy]
        assert "PaymentService" in names  # self
        assert "BaseService" in names  # parent (extends)
        assert "IPaymentProcessor" in names  # parent (implements)
        assert "RefundService" in names  # child (extends)

    def test_hierarchy_up_only(self, graph: kgl.ProjectGraph):
        hierarchy = kgl.get_class_hierarchy(graph, "class:PaymentService", "up")
        depths = [d for d, _, _, _ in hierarchy]
        # Should have self (0) and parents (negative)
        assert 0 in depths
        assert any(d < 0 for d in depths)
        # Should NOT have children (positive)
        assert not any(d > 0 for d in depths)

    def test_hierarchy_down_only(self, graph: kgl.ProjectGraph):
        hierarchy = kgl.get_class_hierarchy(graph, "class:PaymentService", "down")
        names = [name for _, _, name, _ in hierarchy]
        assert "RefundService" in names
        # Should NOT have parents
        assert "BaseService" not in names

    def test_hierarchy_nonexistent(self, graph: kgl.ProjectGraph):
        assert kgl.get_class_hierarchy(graph, "nonexistent") == []


# ---------------------------------------------------------------------------
# Test: Search by Path (P3.3)
# ---------------------------------------------------------------------------

class TestSearchByPath:
    def test_search_by_package(self, graph: kgl.ProjectGraph):
        results = kgl.search_by_path(graph, "src/main")
        assert len(results) > 0
        for node in results:
            assert "src/main" in node.file_path.lower()

    def test_search_by_filename(self, graph: kgl.ProjectGraph):
        results = kgl.search_by_path(graph, "GatewayClient")
        assert len(results) > 0
        assert any(n.name == "GatewayClient.java" for n in results) or \
               any("GatewayClient" in n.file_path for n in results)

    def test_search_with_type_filter(self, graph: kgl.ProjectGraph):
        results = kgl.search_by_path(graph, "Service", node_type="class")
        for node in results:
            assert node.type == "class"

    def test_search_case_insensitive(self, graph: kgl.ProjectGraph):
        results_lower = kgl.search_by_path(graph, "service")
        results_upper = kgl.search_by_path(graph, "SERVICE")
        assert len(results_lower) == len(results_upper)


# ---------------------------------------------------------------------------
# Test: Domain Graph
# ---------------------------------------------------------------------------

class TestDomainGraph:
    def test_domain_node_lookup(self, graph: kgl.ProjectGraph):
        dn = kgl.get_domain_node_by_id(graph, "domain:payment")
        assert dn is not None
        assert dn.name == "Payment Processing"

    def test_domain_children_indexed(self, graph: kgl.ProjectGraph):
        """get_domain_children should use index, not linear scan."""
        flows = kgl.get_domain_children(graph, "domain:payment", "contains_flow")
        assert len(flows) == 1
        assert flows[0][1].name == "Process Payment"

    def test_domain_steps(self, graph: kgl.ProjectGraph):
        steps = kgl.get_domain_children(graph, "flow:pay-process", "has_step")
        assert len(steps) == 2
        names = {s.name for _, s in steps}
        assert "Validate Input" in names
        assert "Execute Payment" in names


# ---------------------------------------------------------------------------
# Test: Language Detection (P2)
# ---------------------------------------------------------------------------

class TestLanguageDetection:
    @pytest.mark.parametrize("path,expected_lang,expected_strategy", [
        ("src/main/Service.java", "java", "brace"),
        ("src/main/app.py", "python", "indent"),
        ("src/main/index.ts", "typescript", "brace"),
        ("config.yaml", "yaml", "none"),
        ("unknown.xyz", "", "brace"),  # default fallback
    ])
    def test_detect_language(self, path, expected_lang, expected_strategy):
        lang, strategy = kgl.detect_language(path)
        assert lang == expected_lang
        assert strategy == expected_strategy


# ---------------------------------------------------------------------------
# Test: Entry Points
# ---------------------------------------------------------------------------

class TestEntryPoints:
    def test_find_entry_points(self, graph: kgl.ProjectGraph):
        entries = kgl.find_entry_points(graph)
        # processPayment is not called by anyone → should be entry point
        names = [n.name for n in entries]
        assert "processPayment" in names
        # validateInput IS called by processPayment → should NOT be entry point
        assert "validateInput" not in names
