"""Driver-level pins for what the TDD loop hands the fill routes.

Two failures this locks out, both observed on anyhow (5 GPU-hours, 74 attempts,
0 fills):
  1. futility escalation named ~50 producers per unresolved test and the SAME
     map was the U2 acceptance oracle → every block in the crate was convicted
     by tests it could not influence, so every compiling fill was reverted;
  2. escalation additions were built from raw fn records AFTER the oracle
     filter had run, so `#[test]` fns whose bodies mention the type were
     proposed as fill targets — the oracle rewriting itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator

_DIRECT = FnLocator("src/a.rs", "accessor")
_ESCALATED = FnLocator("src/b.rs", "producer")
_TEST_FN = FnLocator("src/a.gen_tests_1.rs", "t1")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _deps(handoffs: list[Any]) -> PhaseCTddDeps:
    def _fill(**kw: Any) -> ClusterFillResult:
        handoffs.append(kw.get("tdd_frontier_tests"))
        return ClusterFillResult(attempts=1)

    def _escalate(_src: Path, test: str, _units: set[FnLocator]) -> dict[Any, Any]:
        # A shotgun escalation: a real producer AND a test fn, as the record
        # scan produced on anyhow.
        return {_ESCALATED: {test}, _TEST_FN: {test}}

    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_DIRECT: {"t1"}}, FailureKind.BEHAVIOR, {_DIRECT: {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: {
            k: v for k, v in frontier.items() if "gen_tests" not in k[0]},
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda *a, **k: None,
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (
            101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=_fill,
        escalate_to_producers=_escalate,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def test_escalated_producer_is_dispatched_but_cannot_revert(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    handoffs: list[Any] = []
    _run(_deps(handoffs), tmp_path, monkeypatch)
    escalated = [h for h in handoffs if h is not None and _ESCALATED in h.dispatch]
    assert escalated, "escalation never reached the fill routes"
    fr = escalated[0]
    assert fr.tests_for(_ESCALATED) == {"t1"}
    assert fr.convicting_for(_ESCALATED) == set()
    # The directly-attributed unit keeps its oracle.
    assert fr.convicting_for(_DIRECT) == {"t1"}


def test_escalation_can_never_dispatch_a_test_fn(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    handoffs: list[Any] = []
    _run(_deps(handoffs), tmp_path, monkeypatch)
    assert handoffs, "the fill route was never called"
    for fr in handoffs:
        if fr is None:
            continue
        assert _TEST_FN not in fr.dispatch
        assert _TEST_FN not in fr.convicting
