"""C8: the standalone --phase-*-only / --skeleton-backend handlers relocated
from pipeline.py into the orchestrator, behind PhaseSubcommandDeps."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.orchestrator import (
    PhaseSubcommandDeps,
    run_phase_a,
    run_phase_b,
    run_phase_c,
    run_phase_d,
    run_skeleton_backend,
)


class _Gen:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.req: Any = None

    def generate(self, req: Any) -> bool:
        self.req = req
        return self.ok


def _deps(log: list[str], **over: Any) -> PhaseSubcommandDeps:
    base: dict[str, Any] = {
        "log": lambda m: log.append(m),
        "bake_skeleton_artifacts": lambda pd, mod: {"ok": True, "clusters": ["default"]},
        "collect_test_files": lambda pd: ["tests/a.rs"],
        "group_tests": lambda *a, **k: [
            {"name": "g1", "test_files": ["tests/a.rs"], "target_symbols": ["foo"]}],
        "fill_groups": lambda *a, **k: {"groups_processed": 1, "bytes_filled": 10, "errors": []},
        "cleanup_residuals": lambda *a, **k: {
            "stubs_total": 2, "stubs_exercised": 1, "files_updated": 1, "stubs_remaining": 1},
        "new_skeleton_generator": lambda: _Gen(True),
        "manifest_name": ".generated_files.txt",
    }
    base.update(over)
    return PhaseSubcommandDeps(**base)


def _with_src(tmp_path: Path) -> Path:
    pd = tmp_path / "proj"
    (pd / "src").mkdir(parents=True)
    (pd / "src" / "lib.rs").write_text("fn x() {}\n", encoding="utf-8")
    (pd / "tests").mkdir()
    (pd / "tests" / "a.rs").write_text("#[test] fn t() {}\n", encoding="utf-8")
    return pd


def test_phase_a_no_source_refuses(tmp_path: Path) -> None:
    pd = tmp_path / "proj"
    (pd / "src").mkdir(parents=True)  # empty src
    log: list[str] = []
    assert run_phase_a(_deps(log), pd) == 2
    assert any("no original source is read after strip" in m for m in log)


def test_phase_a_bake_failure(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    log: list[str] = []
    deps = _deps(log, bake_skeleton_artifacts=lambda *a: {"ok": False, "reason": "boom"})
    assert run_phase_a(deps, pd) == 3
    assert any("boom" in m for m in log)


def test_phase_a_success(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    log: list[str] = []
    assert run_phase_a(_deps(log), pd) == 0


def test_phase_b_chains_a_and_groups(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    log: list[str] = []
    assert run_phase_b(_deps(log), pd) == 0
    assert any("[phase-b]" in m for m in log)


def test_phase_b_propagates_phase_a_failure(tmp_path: Path) -> None:
    pd = tmp_path / "proj"
    (pd / "src").mkdir(parents=True)
    assert run_phase_b(_deps([]), pd) == 2


def test_phase_b_no_tests(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    deps = _deps([], collect_test_files=lambda pd: [])
    assert run_phase_b(deps, pd) == 6


def test_phase_c_fills_and_returns_zero(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    log: list[str] = []
    # Pre-create the filled dir to exercise the rmtree branch.
    (pd / ".skeleton-filled").mkdir()
    calls: dict[str, Any] = {}
    deps = _deps(
        log,
        fill_groups=lambda *a, **k: calls.update(k) or {
            "groups_processed": 1, "bytes_filled": 5, "errors": ["e1"]},
    )
    assert run_phase_c(deps, pd) == 0
    assert calls["cluster_name"] == "default"
    assert any("e1" in m for m in log)


def test_phase_d_chains_and_cleans(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    log: list[str] = []
    assert run_phase_d(_deps(log), pd) == 0
    assert any("[phase-d]" in m for m in log)


def test_skeleton_backend_ok_and_synthesizes_missing_docs(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    gen = _Gen(True)
    deps = _deps([], new_skeleton_generator=lambda: gen)
    assert run_skeleton_backend(deps, pd) == 0
    assert (pd / "functional_requirements.md").exists()
    assert (pd / "genesis.md").exists()
    assert gen.req.current_module == "proj"


def test_skeleton_backend_failure_returns_8(tmp_path: Path) -> None:
    pd = _with_src(tmp_path)
    deps = _deps([], new_skeleton_generator=lambda: _Gen(False))
    assert run_skeleton_backend(deps, pd) == 8
