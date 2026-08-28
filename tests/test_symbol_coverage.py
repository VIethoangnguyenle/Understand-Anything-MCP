"""compute_symbol_coverage: separates a real graph from a file-and-import
skeleton. Node-type counts cannot: both show hundreds of `file` nodes."""
import subprocess

import kg_loader as kgl


def _node(ntype, path, name="x"):
    return kgl.Node.from_dict(
        {"id": f"{ntype}:{path}:{name}", "type": ntype, "name": name,
         "filePath": path}
    )


def _graph(nodes, tmp_path=None):
    return kgl.ProjectGraph(
        name="p", root_path=str(tmp_path or "/tmp"), project_info={}, nodes=nodes
    )


def _files(*paths):
    return [_node("file", p) for p in paths]


def test_healthy_graph_reports_ok():
    src = [f"src/m{i}.ts" for i in range(10)]
    nodes = _files(*src) + [_node("function", p, f"f{i}") for i, p in enumerate(src)]
    cov = kgl.compute_symbol_coverage(_graph(nodes))
    assert cov["status"] == "OK"
    assert cov["coverage_pct"] == 100
    assert cov["covered_files"] == 10


def test_skeleton_with_no_symbols_reports_empty():
    """The failure this metric exists to catch: files registered, logic absent."""
    cov = kgl.compute_symbol_coverage(
        _graph(_files(*[f"src/m{i}.ts" for i in range(20)]))
    )
    assert cov["status"] == "EMPTY"
    assert cov["coverage_pct"] == 0
    assert cov["expected_files"] == 20


def test_a_handful_of_symbols_still_reports_low():
    """A graph with 5 symbols across 560 files must not read as healthy."""
    src = [f"src/m{i}.ts" for i in range(100)]
    nodes = _files(*src) + [_node("function", p, "f") for p in src[:5]]
    cov = kgl.compute_symbol_coverage(_graph(nodes))
    assert cov["status"] == "LOW"
    assert cov["coverage_pct"] == 5


def test_barrels_types_and_styles_leave_the_denominator():
    """Otherwise every healthy TypeScript project scores as broken."""
    real = [f"src/m{i}.tsx" for i in range(6)]
    excluded = [
        "src/index.ts", "src/modules/a/index.tsx", "src/global.d.ts",
        "src/a.types.ts", "src/b.enum.ts", "src/c.constants.ts",
        "src/d.contracts.ts", "vite.config.ts", "src/styles/main.scss",
        "src/theme.css", "package.json", "README.md", "logo.svg",
    ]
    nodes = _files(*real, *excluded) + [_node("function", p, "C") for p in real]
    cov = kgl.compute_symbol_coverage(_graph(nodes))
    assert cov["expected_files"] == 6
    assert cov["total_files"] == 6 + len(excluded)
    assert cov["status"] == "OK"
    assert cov["coverage_pct"] == 100


def test_class_and_method_nodes_count_as_symbols():
    src = [f"src/m{i}.py" for i in range(6)]
    nodes = _files(*src) + [
        _node("class", src[0]), _node("method", src[1]),
        _node("function", src[2]), _node("class", src[3]),
        _node("function", src[4]), _node("method", src[5]),
    ]
    assert kgl.compute_symbol_coverage(_graph(nodes))["coverage_pct"] == 100


def test_too_few_source_files_is_unknown_not_zero():
    """A docs-only repo must not be reported as a broken graph."""
    cov = kgl.compute_symbol_coverage(_graph(_files("README.md", "src/a.ts")))
    assert cov["status"] == "UNKNOWN"
    assert cov["coverage_pct"] == -1


def test_tsx_change_marks_graph_stale():
    """`.ts` without `.tsx` let UI-only work slip past the freshness check."""
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True)

    import tempfile, pathlib
    tmp = tempfile.mkdtemp()
    git("init")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
        "--allow-empty", "-m", "base")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp,
                          capture_output=True, text=True, check=True).stdout.strip()
    pathlib.Path(tmp, "Button.tsx").write_text("export const Button = () => null\n")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "ui")

    g = kgl.ProjectGraph(name="p", root_path=tmp, project_info={})
    g.git_commit_hash = head
    fresh = kgl.check_freshness(g)
    assert fresh["stale_file_count"] == 1
    assert "Button.tsx" in fresh["stale_files_sample"]
    assert fresh["is_stale"] is True
