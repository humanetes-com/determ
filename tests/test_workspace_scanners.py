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

from pipeline_core.adapters import workspace_scan as ws_mod
from pipeline_core.adapters.workspace_scan import (
    LLMDependencyGraph,
    LLMWorkspaceScanner,
    StaticDependencyGraph,
    StaticWorkspaceScanner,
    WorkspaceScanDeps,
)


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


def _graph(tmp_path, monkeypatch) -> StaticDependencyGraph:
    ws = _workspace(tmp_path)
    # Force the tomllib fallback (don't depend on a real cargo binary/workspace).
    monkeypatch.setattr(StaticDependencyGraph, "_try_cargo_metadata",
                        lambda self: False)
    return StaticDependencyGraph(ws)


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
    out = StaticWorkspaceScanner().scan(ws)
    assert "Crate `crate_a`" in out and "Crate `crate_b`" in out
    assert "lib target" in out          # crate_a
    assert "proc-macro target" in out   # crate_b
    # crate_a's external dep is surfaced under its dependencies block.
    assert "serde" in out
    # Empty project → empty string.
    assert StaticWorkspaceScanner().scan(tmp_path / "nonexistent") == ""


def test_llm_workspace_scanner_uses_injected_ask(tmp_path) -> None:
    """The LLM scanner collects manifests and wraps the model's reply; `ask` is
    passed via WorkspaceScanDeps (#14), faked here so no live model is needed."""
    ws = _workspace(tmp_path)
    seen = {}

    def fake_ask(prompt, **k):
        seen["p"] = prompt
        return "**Package `crate_a`**: src/lib.rs"

    deps = WorkspaceScanDeps(ask=fake_ask, task0_model="m")
    out = LLMWorkspaceScanner(deps).scan(ws)
    assert "Workspace structure" in out and "**Package `crate_a`**" in out
    # The collected manifests were fed to the model.
    assert "Cargo.toml" in seen["p"]
    # No manifests → empty string (no model call needed).
    assert LLMWorkspaceScanner(deps).scan(tmp_path / "nonexistent") == ""


def test_llm_dep_graph_parses_model_response(tmp_path) -> None:
    """`_parse_response` turns the structured model reply into the graph: module
    list, pruned intra-workspace deps, topo order, and command templates."""
    g = LLMDependencyGraph(tmp_path / "empty")  # no manifests → no model call
    parsed = g._parse_response(
        "MODULE: name=crate_a dir=crate_a type=lib deps=\n"
        "MODULE: name=crate_b dir=crate_b type=proc-macro deps=crate_a,serde\n"
        "TOPO_ORDER: crate_a crate_b\n"
        "MODULE_TEST_CMD: cargo test -p {module}\n"
        "MODULE_BUILD_CHECK: NONE\n"
    )
    assert parsed is not None
    modules, deps, types, topo, test_tpl, check_tpl = parsed
    assert set(modules) == {"crate_a", "crate_b"}
    assert types["crate_b"] == "proc-macro"
    assert deps["crate_b"] == {"crate_a"}      # external `serde` pruned
    assert topo == ["crate_a", "crate_b"]
    assert test_tpl == "cargo test -p {module}"
    assert check_tpl == ""                       # NONE → empty
    # Unparseable reply → None (caller falls back to whole-workspace mode).
    assert g._parse_response("no modules here") is None


def test_scanner_factories_honor_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKSPACE_SCANNER", "static")
    assert isinstance(ws_mod._workspace_scanner_factory(), StaticWorkspaceScanner)
    monkeypatch.setenv("WORKSPACE_SCANNER", "llm")
    assert isinstance(ws_mod._workspace_scanner_factory(), LLMWorkspaceScanner)
    monkeypatch.setenv("DEPENDENCY_GRAPH", "static")
    assert isinstance(ws_mod._dependency_graph_factory(tmp_path / "x"), StaticDependencyGraph)
    monkeypatch.setenv("DEPENDENCY_GRAPH", "llm")
    assert isinstance(ws_mod._dependency_graph_factory(tmp_path / "x"), LLMDependencyGraph)


def test_try_cargo_metadata_parses_workspace(tmp_path, monkeypatch) -> None:
    """The authoritative cargo-metadata path (workspace_scan.py:304-354): faked
    `cargo metadata` JSON populates modules/crate-types and resolves intra-workspace
    deps, dropping externals + tests-fixture + out-of-tree + hyphen/underscore variants."""
    import json
    from types import SimpleNamespace

    ws = tmp_path / "ws"
    for sub in ("a", "b", "c", "x_y", "tests/helper"):
        (ws / sub).mkdir(parents=True)

    def mf(sub: str) -> str:
        return str(ws / sub / "Cargo.toml")

    payload = {
        "workspace_members": ["a", "b", "c", "x_y", "thelper", "missing", "outside"],
        "packages": [
            # a: lib, depends on workspace `b` + external serde (dropped) + `x-y` (hyphen variant).
            {"id": "a", "name": "a", "manifest_path": mf("a"), "targets": [{"kind": ["lib"]}],
             "dependencies": [{"name": "b"}, {"name": "serde"}, {"name": "x-y"}]},
            {"id": "b", "name": "b", "manifest_path": mf("b"),
             "targets": [{"kind": ["proc-macro"]}], "dependencies": []},
            {"id": "c", "name": "c", "manifest_path": mf("c"),
             "targets": [{"kind": ["bin"]}], "dependencies": []},
            {"id": "x_y", "name": "x_y", "manifest_path": mf("x_y"),
             "targets": [{"kind": ["lib"]}], "dependencies": []},
            # under tests/ → skipped.
            {"id": "thelper", "name": "helper", "manifest_path": mf("tests/helper"),
             "targets": [{"kind": ["lib"]}], "dependencies": []},
            # manifest outside project_dir → relative_to ValueError → skipped.
            {"id": "outside", "name": "outside", "manifest_path": "/elsewhere/Cargo.toml",
             "targets": [{"kind": ["lib"]}], "dependencies": []},
            # "missing" has no package entry → continue.
        ],
    }
    monkeypatch.setattr(ws_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))
    g = StaticDependencyGraph(ws)
    assert sorted(g.modules()) == ["a", "b", "c", "x_y"]      # helper/outside/missing excluded
    assert g.crate_type("a") == "lib"
    assert g.crate_type("b") == "proc-macro"
    assert g.crate_type("c") == "bin"
    assert g.workspace_deps("a") == {"b", "x_y"}              # serde dropped; x-y → x_y resolved
    assert g.workspace_deps("b") == set()


def test_try_cargo_metadata_failure_falls_back(tmp_path, monkeypatch) -> None:
    """Non-zero cargo / bad JSON / no members → returns False so _parse uses the
    tomllib manifest parser instead (workspace_scan.py:298-302, 340-341)."""
    from types import SimpleNamespace

    ws = _workspace(tmp_path)  # has real Cargo.toml files for the fallback parser
    monkeypatch.setattr(ws_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no cargo"))
    g = StaticDependencyGraph(ws)
    # Fallback tomllib parser still found the crates.
    assert sorted(g.modules()) == ["crate_a", "crate_b"]
