"""build_graph_metadata: one structured, JSON-safe snapshot of graph state
(identity, counts, graph commit, repository HEAD, freshness) — the machine
counterpart of the human get_graph_stats text."""
import json
import subprocess

import kg_loader as kgl


def _git(tmp_path, *args):
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def _repo_with_graph(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "--allow-empty", "-m", "init")
    return kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={})


def test_metadata_shape_and_json_safety(tmp_path):
    g = _repo_with_graph(tmp_path)
    meta = kgl.build_graph_metadata(g)
    encoded = json.dumps(meta)  # must be JSON-serializable
    assert json.loads(encoded)["contract_version"] == 1
    assert meta["project"] == "p"
    assert meta["root_path"] == str(tmp_path)
    for key in ("node_count", "edge_count", "domain_node_count", "graph_commit",
                "analyzed_at"):
        assert key in meta["graph"]
    assert set(meta["freshness"]) >= {"status", "stale_file_count",
                                      "stale_files_sample", "git_commit_hash"}


def test_repository_head_matches_git(tmp_path):
    g = _repo_with_graph(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    meta = kgl.build_graph_metadata(g)
    assert meta["repository"]["head"] == head


def test_head_empty_outside_git_repo(tmp_path):
    g = kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={})
    meta = kgl.build_graph_metadata(g)
    assert meta["repository"]["head"] == ""
    assert meta["freshness"]["status"] == "UNKNOWN"


def _n(i):
    return kgl.Node.from_dict({"id": f"file:f{i}", "type": "file", "name": f"f{i}",
                               "filePath": f"src/f{i}.py"})


def test_health_degraded_for_edgeless_skeleton(tmp_path):
    """Dogfood F1/F2: a fabricated graph (many nodes, zero edges) must not
    present as healthy."""
    g = kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={},
                         nodes=[_n(1), _n(2), _n(3)])
    meta = kgl.build_graph_metadata(g)
    assert meta["health"]["status"] == "DEGRADED"
    assert any("no edges" in w for w in meta["health"]["warnings"])


def test_health_warns_on_missing_analyzed_at(tmp_path):
    g = kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={},
                         nodes=[_n(1), _n(2)],
                         edges=[kgl.Edge.from_dict({"source": "file:f1",
                                                    "target": "file:f2",
                                                    "type": "imports"})])
    meta = kgl.build_graph_metadata(g)
    assert meta["health"]["status"] == "HEALTHY"  # edges exist
    assert any("analyzedAt" in w for w in meta["health"]["warnings"])


def test_health_clean_for_real_graph(tmp_path):
    g = kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={},
                         nodes=[_n(1), _n(2)],
                         edges=[kgl.Edge.from_dict({"source": "file:f1",
                                                    "target": "file:f2",
                                                    "type": "imports"})],
                         analyzed_at="2026-07-12T00:00:00Z")
    meta = kgl.build_graph_metadata(g)
    assert meta["health"] == {"status": "HEALTHY", "warnings": []}
