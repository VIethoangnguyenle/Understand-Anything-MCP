"""_resolve_file_path must never read outside the project root (or an
UPSTREAM_ROOTS entry for upstream: nodes) — graphs are agent-generated JSON
and must be treated as untrusted input."""
import os

import pytest

import kg_loader as kgl


def _graph(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.java").write_text("class Ok {}", encoding="utf-8")
    return kgl.ProjectGraph(name="p", root_path=str(tmp_path), project_info={})


def _node(node_id, file_path):
    # Node has no field defaults — from_dict fills them.
    return kgl.Node.from_dict({"id": node_id, "type": "file", "name": "n",
                               "filePath": file_path})


def test_legit_relative_path_resolves(tmp_path):
    g = _graph(tmp_path)
    got = kgl._resolve_file_path(g, _node("file:ok", "src/ok.java"))
    assert got == os.path.realpath(os.path.join(str(tmp_path), "src", "ok.java"))


def test_absolute_path_rejected(tmp_path):
    g = _graph(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    assert kgl._resolve_file_path(g, _node("file:x", str(outside))) is None


def test_dotdot_escape_rejected(tmp_path):
    g = _graph(tmp_path)
    (tmp_path.parent / "escape.txt").write_text("s", encoding="utf-8")
    assert kgl._resolve_file_path(g, _node("file:x", "../escape.txt")) is None


def test_symlink_escape_rejected(tmp_path):
    g = _graph(tmp_path)
    outside = tmp_path.parent / "target.txt"
    outside.write_text("s", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert kgl._resolve_file_path(g, _node("file:x", "link.txt")) is None


def test_nul_byte_rejected(tmp_path):
    g = _graph(tmp_path)
    assert kgl._resolve_file_path(g, _node("file:x", "src/ok\x00.java")) is None


def test_upstream_root_containment(tmp_path, monkeypatch):
    g = _graph(tmp_path)
    upstream = tmp_path.parent / "upstream"
    (upstream / "lib").mkdir(parents=True)
    (upstream / "lib" / "u.java").write_text("class U {}", encoding="utf-8")
    monkeypatch.setenv("UPSTREAM_ROOTS", str(upstream))
    ok = kgl._resolve_file_path(g, _node("upstream:u", "lib/u.java"))
    assert ok == os.path.realpath(os.path.join(str(upstream), "lib", "u.java"))
    (tmp_path.parent / "beyond.txt").write_text("s", encoding="utf-8")
    assert kgl._resolve_file_path(g, _node("upstream:x", "../beyond.txt")) is None
