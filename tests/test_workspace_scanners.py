"""Characterization tests for the deterministic workspace scanners (P4 net).

`StaticWorkspaceScanner` (markdown structure for TASK 2) and
`StaticDependencyGraph` (module list / topo order / module-scoped commands) have
no behavioural tests today — these pin their observable output so the P4
relocation to adapters/ is a verbatim move check.sh can prove. The cargo-metadata
path (subprocess) is forced off so the deterministic tomllib parser is exercised
(also covers the R8 regex→tomllib dep-parser migration).
"""
from __future__ import annotations

from pathlib import Path

import pipeline


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "crate_a" / "src").mkdir(parents=True)
    (ws / "crate_b" / "src").mkdir(parents=True)
    (ws / "crate_a" / "Cargo.toml").write_text(
        '[package]\nname = "crate_a"\nversion = "0.1.0"\n\n'
        '[dependencies]\nserde = "1.0"\n'
    )
    (ws / "crate_a" / "src" / "lib.rs").write_text("pub fn a() {}\n")
    # crate_b is a proc-macro and depends on crate_a (intra-workspace edge).
    (ws / "crate_b" / "Cargo.toml").write_text(
        '[package]\nname = "crate_b"\nversion = "0.1.0"\n\n'
        '[lib]\nproc-macro = true\n\n'
        '[dependencies]\ncrate_a = { path = "../crate_a" }\n'
    )
    (ws / "crate_b" / "src" / "lib.rs").write_text("pub fn b() {}\n")
    return ws


def _graph(tmp_path, monkeypatch) -> pipeline.StaticDependencyGraph:
    ws = _workspace(tmp_path)
    # Force the tomllib fallback (don't depend on a real cargo binary/workspace).
    monkeypatch.setattr(pipeline.StaticDependencyGraph, "_try_cargo_metadata",
                        lambda self: False)
    return pipeline.StaticDependencyGraph(ws)


def test_static_dep_graph_modules_types_and_deps(tmp_path, monkeypatch) -> None:
    g = _graph(tmp_path, monkeypatch)
    assert sorted(g.modules()) == ["crate_a", "crate_b"]
    assert g.crate_type("crate_a") == "lib"
    assert g.crate_type("crate_b") == "proc-macro"
    # Intra-workspace edge resolved; the external `serde` dep is pruned out.
    assert g.workspace_deps("crate_b") == {"crate_a"}
    assert g.workspace_deps("crate_a") == set()


def test_static_dep_graph_topological_order(tmp_path, monkeypatch) -> None:
    g = _graph(tmp_path, monkeypatch)
    # Dependency precedes dependent: crate_a before crate_b.
    assert g.topological_order() == ["crate_a", "crate_b"]


def test_static_dep_graph_module_commands(tmp_path, monkeypatch) -> None:
    g = _graph(tmp_path, monkeypatch)
    assert g.module_test_command("cargo test", "crate_a") == "cargo test -p crate_a"
    assert (g.module_test_command("cargo test --all-features", "crate_a")
            == "cargo test -p crate_a --all-features")
    assert (g.module_test_command("cargo nextest run", "crate_b")
            == "cargo nextest run -p crate_b")
    assert (g.module_build_check_command("cargo check --workspace", "crate_a")
            == "cargo check -p crate_a")
    assert (g.module_build_check_command("cargo check", "crate_b")
            == "cargo check -p crate_b")
    # Non-cargo command passes through unchanged.
    assert g.module_test_command("make test", "crate_a") == "make test"


def test_static_workspace_scanner_markdown(tmp_path) -> None:
    ws = _workspace(tmp_path)
    out = pipeline.StaticWorkspaceScanner().scan(ws)
    assert "Crate `crate_a`" in out and "Crate `crate_b`" in out
    assert "lib target" in out          # crate_a
    assert "proc-macro target" in out   # crate_b
    # crate_a's external dep is surfaced under its dependencies block.
    assert "serde" in out
    # Empty project → empty string.
    assert pipeline.StaticWorkspaceScanner().scan(tmp_path / "nonexistent") == ""
