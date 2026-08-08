"""The plateau budget must not be charged for an iteration that filled stubs.

A cluster early in its climb pays in STUBS and is paid back in TESTS several
iterations later: nothing flips until a whole producer chain is complete. On
2026-08-07 cluster `stable` of run `7445-00ee8a3c` filled 36 then 26 fns across
its first three iterations — stub count 132 -> 118 -> 82 -> 75 — while the pass
count went 11 -> 13 -> 13, and iter 3 was charged `stall=1/3`. Two more such
iterations would have ended task2a for that cluster with 75 `unimplemented!()`
bodies still on disk.
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

# Flat sweep: the same test fails every iter, so `pass_count` never improves.
_FLAT = "test t1 ... FAILED\ntest result: FAILED. 3 passed; 1 failed"
_TARGET = FnLocator("src/a.rs", "producer")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _stubs(n: int) -> list[FnLocator]:
    return [FnLocator("src/a.rs", f"stub{i}") for i in range(n)]


def _run(tmp_path: Path, monkeypatch, counts: list[int],
         events: list[tuple[str, dict]]) -> ClusterFillResult:
    """Drive the loop over a GREEN, flat-scoring tree whose stub count follows
    `counts`; the last value repeats for any call past the end."""
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", "3")
    seq = iter(counts)
    last = [counts[0]]

    def _scan(*a: Any, **k: Any) -> list[FnLocator]:
        last[0] = next(seq, last[0])
        return _stubs(last[0])

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_TARGET: {"t1"}}, FailureKind.RUNTIME, {_TARGET: {"t1"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts_: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: 0,
        run_cargo=lambda *a, **k: (101, _FLAT),
        scan_unimplemented_for_cluster=_scan,
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(attempts=1),
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def _iters(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.iter"]


def test_a_shrinking_stub_set_is_not_a_plateau(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    # Three sweeps that each see fewer stubs than the last, then a flat tail.
    _run(tmp_path, monkeypatch, [10, 8, 6, 4, 4, 4, 4, 4], events)

    banked = [d for ev, d in events if ev == "phase_c.tdd.stub_progress"]
    assert [(d["stubs_prev"], d["stubs_now"]) for d in banked] == [
        (10, 8), (8, 6), (6, 4)], \
        "each sweep that shrank the stub set must spare the plateau budget"

    iters = _iters(events)
    assert iters[-1]["kind"] == "stall_exit", "the exit must still be reachable"
    # Charged only once the stub count went flat: iters 5/6/7 of a run that
    # would have exited at iter 4 on the pass count alone.
    assert [d["stall"] for d in iters[-3:]] == [1, 2, 3]
    assert len(iters) == 7


def test_a_flat_stub_set_still_exits_on_the_pass_count(
        tmp_path: Path, monkeypatch) -> None:
    """The grace is bought with stubs, not with activity: a loop that fills
    nothing plateaus on exactly the budget it always did."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [10], events)

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.stub_progress"]
    iters = _iters(events)
    assert len(iters) == 4 and iters[-1]["kind"] == "stall_exit"


def test_a_growing_stub_set_is_charged(tmp_path: Path, monkeypatch) -> None:
    """A retrigger re-stubs bodies, which RAISES the count. That is not
    progress the oracle cannot see yet — it is the tree moving backwards — so
    it buys nothing (the retrigger's own grace window covers that case)."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [4, 6, 8, 10], events)

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.stub_progress"]
    iters = _iters(events)
    assert len(iters) == 4 and iters[-1]["kind"] == "stall_exit"
