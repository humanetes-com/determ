"""Measured-owner admission runs before the empty-frontier decision.

A leftover whose whole attributed cohort is exhausted, on an otherwise empty
frontier, has owners the coverage reading names. The loop used to return to
the sweep before the owner block whenever nothing was eligible, so that
shape spent a no-other-move re-admission slot and, after
`RETRO_RESET_MAX_ATTEMPTS`, stopped with its owners never dispatched (the
remainder recorded after run `3040-51eab48c`). Invented crate:
`a` is the attributed stub, `b` a filled body only the coverage reading
names as the test's owner (the shape of run `450922-661c5e9f`'s four tests
listed under nobody, whose owners were filled bodies).

Iteration 1 dispatches `a`; the fill leaves it a stub, so it is exhausted.
Iteration 2 has an empty eligible set. On theory the owner `b` is admitted
and dispatched there, and no re-admission is spent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory

_A = FnLocator("src/a.rs", "a")
_B = FnLocator("src/b.rs", "b")


def _cluster() -> BuildCluster:
    return BuildCluster(name="c", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def test_an_empty_frontier_admits_measured_owners_before_spending_a_readmission(
        tmp_path: Path, monkeypatch: Any) -> None:
    handoffs: list[frozenset[FnLocator]] = []
    events: list[tuple[str, Any]] = []
    measured: list[int] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        fr = kw.get("tdd_frontier_tests")
        keys = frozenset(fr.dispatch) if fr is not None else frozenset()
        handoffs.append(keys)
        # Every dispatched unit was attempted and is still a stub.
        return ClusterFillResult(
            attempts=1, dispatched_fns=tuple((k.file, k.fn_name) for k in keys))

    def _coverage(*_a: Any, **_k: Any) -> CoverageSlice:
        measured.append(1)
        return CoverageSlice(owners={"t1": frozenset({FnSite(_B.file, _B.fn_name, "")})})

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_A: {"t1"}}, FailureKind.BEHAVIOR, {FnSite(_A.file, _A.fn_name, ""): {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda fr, src, **_k: dict(fr),
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, *a, **k: events.append((ev, k.get("data"))),
        log=lambda m, *a, **k: None,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=lambda *a, **k: (
            101, "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"),
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory.of_units([_A]),
        scan_unimplemented_functions=lambda *a, **k: StubInventory.of_units([_A]),
        task2_fill_fns_one_by_one=_fill,
        measure_coverage_slice=_coverage,
    )
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "0")
    monkeypatch.setenv("TDD_NO_FRONTIER_STOP", "1")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("PHASE_C_TDD_BACKSTOP", "4")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[c]")

    # The stub stays fillable. Its edge to `t1` is a guess the sweep did
    # not observe (coverage says `t1` entered `b` only), so `t1`'s cohort
    # is empty and, `t1` being seen, its measured owner is admitted on the
    # first sweep: no later than the empty-frontier sweep this test was
    # written for.
    assert _A in handoffs[0]
    first_b = next((i for i, h in enumerate(handoffs) if _B in h), None)
    assert first_b is not None and first_b <= 1, (
        f"the owner is dispatched by the empty-frontier sweep: {handoffs}")
    names = [ev for ev, _d in events]
    owner_at = names.index("phase_c.attribution.measured_owners")
    assert "phase_c.tdd.no_move_readmission" not in names[:owner_at], (
        "no re-admission slot is spent before the owners are admitted")
    # The empty-frontier sweep measured coverage on a tree the scripted
    # fill never changed: the reading is reused, not paid again.
    assert len(measured) < sum(1 for ev in names if ev == "phase_c.coverage.owner_map") + 1
