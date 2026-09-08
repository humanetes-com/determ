"""A sweep that HANGS is a reading the loop scores, never a reason to leave
the cluster — pinned through the real `run_phase_c_tdd`.

Run `3371694-c8835690` (2026-09-08, cluster `stable`): the entry sweep of a
resumed run timed out inside `tests/ptr_gen_1.rs::test_error_debug_chain`
(27 passed / 10 failed reported by the binaries before it), the loop logged
`cargo test TIMED OUT (120s). Stopping.`, broke out of the cluster and went
on to nightly at 0 passing — with a durable best tree of 91 on disk one
directory away. The plan's sentences:

- a timeout that cut the run INSIDE a test is a hung binary: the sweep
  names it, counts its tests and every binary after it as failing, and
  continues the iteration;
- in a process with no batch snapshot (the entry sweep) whose durable mark
  beats the hung reading's floor, the durable pair is restored once and
  the tree re-swept in the same iteration.

Negative controls: a timeout that cut the BUILD (no section opened) still
ends the cluster `timeout`, the pre-fix contract for that case; and with
no durable restore wired the hang is still scored, not skipped.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
from pipeline_core.domain.best_tree_mark import BestTreeMark
from pipeline_core.domain.models import BuildCluster
from tests.test_tdd_ratchet import _driver_deps

_NAMES = ("x", "y", "test_error_context_display", "test_error_debug_chain",
          "test_z", "later")
_LIB = ("    Running unittests src/lib.rs (t/lib-1)\n"
        "running 2 tests\ntest x ... ok\ntest y ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored\n")
_HUNG = (_LIB
         + "    Running tests/ptr_gen_1.rs (t/ptr_gen_1-1)\n"
           "running 3 tests\n"
           "test test_error_context_display ... FAILED\n"
           "test test_error_debug_chain ... \n"
           "\nTIMEOUT after 120s")
_HEALTHY = (_LIB
            + "    Running tests/ptr_gen_1.rs (t/ptr_gen_1-1)\n"
              "running 3 tests\ntest test_error_context_display ... FAILED\n"
              "test test_error_debug_chain ... ok\ntest test_z ... ok\n"
              "test result: FAILED. 2 passed; 1 failed; 0 ignored\n"
              "    Running tests/later.rs (t/later-1)\n"
              "running 1 test\ntest later ... ok\n"
              "test result: ok. 1 passed; 0 failed; 0 ignored\n")
_BUILD_CUT = "   Compiling anyhow v1.0.0\n\nTIMEOUT after 120s"


def _tree(tmp_path: Path) -> None:
    for rel in (".skeleton/stable/src", "src"):
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.rs").write_text("pub fn depth() -> usize { 1 }\n", encoding="utf-8")


def _run(monkeypatch, tmp_path: Path, captures: list[tuple[int, str]], *,
         mark: BestTreeMark | None, restore: bool) -> tuple[list, list, list]:
    _tree(tmp_path)
    for k, v in (("TDD_MAX_STALLS", "1"), ("TDD_FLAT_SCORE_REPEATS", "0"),
                 ("TDD_NO_VERDICT_SWEEPS", "0"), ("TDD_FILL_PHASE_MAX_MINUTES", "0"),
                 ("SLICE_SESSION_MAX_PER_FILL", "0"), ("TDD_NO_FRONTIER_STOP", "1")):
        monkeypatch.setenv(k, v)
    calls: list[int] = []
    restores: list[tuple[Any, ...]] = []
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        rc, out = captures[min(len(calls), len(captures) - 1)]
        calls.append(1)
        return rc, out

    def _restore(pd: Path, cl: str, filled: Path) -> tuple[int, int, list[str]]:
        restores.append((pd, cl, filled))
        return 3, 1, []

    deps = _driver_deps(
        _cargo, lambda pd, cl, stems, lib=False: (len(_NAMES), frozenset(_NAMES)),
        events, None, best_mark=mark, logs=logs, stubs=[], tree_stubs=[])
    if restore:
        deps = replace(deps, restore_best_tree_pair=_restore)
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")
    return events, logs, restores


def _iters(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.iter"]


def test_a_hung_entry_sweep_restores_the_durable_pair_and_continues(monkeypatch, tmp_path):
    mark = BestTreeMark(pass_count=5, iteration=3, tree_sha="deadbeef", oracle_size=6)
    events, logs, restores = _run(
        monkeypatch, tmp_path, [(-1, _HUNG), (101, _HEALTHY)], mark=mark, restore=True)
    assert len(restores) == 1 and restores[0][1] == "stable"
    hung_line = next(ln for ln in logs if "cargo test rc=-1 HUNG in" in ln)
    assert "tests/ptr_gen_1.rs::test_error_debug_chain" in hung_line
    restored = [d for ev, d in events if ev == "phase_c.tdd.hung_tree_restored"]
    assert restored and restored[0]["hung_in"] == "tests/ptr_gen_1.rs::test_error_debug_chain"
    assert restored[0]["pass_count"] == 2 and restored[0]["mark_pass"] == 5
    its = _iters(events)
    assert its and all(d.get("kind") != "timeout" for d in its)
    # The iteration's reading is the re-sweep of the restored tree.
    assert its[0]["pass_count"] == 5 and its[0]["hung"] == []
    assert any("hung tree restored" in ln for ln in logs)


def test_a_hung_sweep_without_a_durable_tier_is_still_scored_not_skipped(monkeypatch, tmp_path):
    events, logs, restores = _run(
        monkeypatch, tmp_path, [(-1, _HUNG), (-1, _HUNG)], mark=None, restore=False)
    assert restores == []
    its = _iters(events)
    assert its and all(d.get("kind") != "timeout" for d in its)
    first = its[0]
    assert first["hung"] == ["tests/ptr_gen_1.rs"]
    assert first["hung_tests"] == ["tests/ptr_gen_1.rs::test_error_debug_chain"]
    # The floor is the completed binaries' passes (2). The three tests that
    # reported no verdict — the hung one, the one after it in the hung
    # binary, and the binary that never ran — are counted failing; the
    # FAILED verdict printed inside the hung section is a name for
    # attribution (`failed`), not a trailer count, the same rule a crashed
    # section's partial verdicts already follow.
    assert first["pass_count"] == 2 and first["fail_count"] == 3
    assert "test_error_context_display" in first["failed"]
    assert {"test_error_debug_chain", "test_z", "later"}.isdisjoint(first["passed"])
    assert any("cargo test rc=-1 HUNG in" in ln for ln in logs)


def test_a_timeout_that_cut_the_build_still_ends_the_cluster(monkeypatch, tmp_path):
    """Negative control: the pre-fix contract for a build that did not
    finish — no section opened, no reading, the cluster stops."""
    mark = BestTreeMark(pass_count=5, iteration=3, tree_sha="deadbeef", oracle_size=6)
    events, logs, restores = _run(
        monkeypatch, tmp_path, [(-1, _BUILD_CUT)], mark=mark, restore=True)
    assert restores == []
    its = _iters(events)
    assert len(its) == 1 and its[0]["kind"] == "timeout"
    assert any("the build did not finish. Stopping." in ln for ln in logs)
    assert not any("HUNG in" in ln for ln in logs)
    assert os.getenv("TDD_MAX_STALLS") == "1"
