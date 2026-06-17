"""Unit tests for the WorkspaceArtifacts adapter (P6 prerequisite slice) — the
read-side workspace artefacts moved out of pipeline.py's free functions +
_ReconstructAdapter binding."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.artifacts import WorkspaceArtifacts, api_surface_path
from pipeline_core.domain.workspace_layout import LOCKED_MODULES_DIR


def test_load_manifest_skips_blanks(tmp_path: Path) -> None:
    m = tmp_path / "manifest.txt"
    m.write_text("src/a.rs\n\n  \nsrc/b.rs\n")
    assert WorkspaceArtifacts().load_manifest(m) == ["src/a.rs", "src/b.rs"]
    assert WorkspaceArtifacts().load_manifest(tmp_path / "absent.txt") == []


def test_load_api_surface_present_and_absent(tmp_path: Path) -> None:
    art = WorkspaceArtifacts()
    assert art.load_api_surface(tmp_path, "mod") == ""
    p = api_surface_path(tmp_path, "mod")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pub fn f();")
    assert art.load_api_surface(tmp_path, "mod") == "pub fn f();"


def test_load_locked_source_context(tmp_path: Path) -> None:
    art = WorkspaceArtifacts()
    assert art.load_locked_source_context(tmp_path, set()) == ""
    lock = tmp_path / LOCKED_MODULES_DIR / "dep"
    lock.mkdir(parents=True)
    (lock / "lib.rs").write_text("pub struct T;")
    (lock / "locked.txt").write_text("ignored")          # ledger excluded
    out = art.load_locked_source_context(tmp_path, {"dep"})
    assert "dep/lib.rs" in out and "pub struct T;" in out
    assert "locked.txt" not in out


def test_format_whole_project_context() -> None:
    art = WorkspaceArtifacts()
    assert art.format_whole_project_context([], "x") == ""
    out = art.format_whole_project_context([("t.rs", "fn t() {}")], "0-test module")
    assert "WHOLE-PROJECT CONTEXT: 0-test module" in out
    assert '<TESTFILE path="t.rs">' in out


def test_collect_source_context_excludes(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text("A")
    (tmp_path / "b.rs").write_text("B")
    m = tmp_path / "manifest.txt"
    m.write_text("a.rs\nb.rs\n")
    out = WorkspaceArtifacts().collect_source_context(tmp_path, m, exclude={"b.rs"})
    assert '<FILE path="a.rs">' in out and "A" in out
    assert "b.rs" not in out
