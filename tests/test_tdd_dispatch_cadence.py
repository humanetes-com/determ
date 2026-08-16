"""Driver pins for the dispatch cap and the sweep taken on the way out.

Both answer measurements from 2026-08-15. The cap answers a sweep period of
198-253 minutes against run lifetimes of 214-458 minutes — one or two readings
per run (`500301-6a66bf86` took exactly one in 214m10s, `669347-cf33eae3` one
in its first 4h17m). The final sweep answers what that cost: `500301-6a66bf86`
last measured `pass=39` with 3 crashed binaries, landed its re-fills, and left
on the stop sentinel with no sweep in between; the next run opened on the
result and read `pass=9` with 10 crashed binaries, and the batch ratchet — the
gate whose whole job is that drop — never got a reading to fire on.

The cap's own hazard has its pin here too, as the pair below. Narrowing the
batch without re-denominating the stall budget would fire the plateau exit
(frontier/cap) times sooner in wall-clock, which is why the uncapped arm is the
negative control rather than a second happy path: it reproduces the pre-cap
iteration count, and the capped arm must spend the SAME number of frontier
traversals before giving up.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator

_FRONTIER = [FnLocator("src/a.rs", f"fn{i}") for i in range(30)]

_FLAT = ("    Running tests/t.rs (t/t-1)\n"
         "running 4 tests\n"
         "test t1 ... ok\n"
         "test t2 ... FAILED\ntest t3 ... FAILED\ntest t4 ... FAILED\n"
         "test result: FAILED. 1 passed; 3 failed")


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _deps(*, logs: list[str], batches: list[tuple[FnLocator, ...]],
          cargo_calls: list[int], fill=None) -> PhaseCTddDeps:
    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        cargo_calls.append(1)
        return 101, _FLAT

    def _fill(**kw: Any) -> ClusterFillResult:
        batches.append(tuple(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1)

    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {f: {"t2"} for f in _FRONTIER}, FailureKind.RUNTIME,
            {f: {"t2"} for f in _FRONTIER}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda *a, **k: None,
        log=logs.append,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=fill or _fill,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch, *,
         cap: str, max_stalls: str = "3", guard: str = "0") -> ClusterFillResult:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    # The flat-score GPU guard would stop these runs at their second
    # identical reading, which is exactly what it is for and exactly what
    # this module is not about: the mechanisms under test here sit BELOW
    # it in the same iteration (the plateau budget, the ratchet, the
    # conviction rules), so the guard is off and their controls keep
    # measuring what they were written to measure. `tests/test_tdd_gpu_guard.py`
    # owns the guard itself — and the last case in THIS module turns it on,
    # because "each mechanism is tested with the other one off" is how the two
    # shipped in the same batch with no coverage of the configuration a launch
    # actually runs.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", guard)
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", cap)
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def test_uncapped_flat_run_exits_after_max_stalls_iterations(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL — the pre-cap loop. One iteration is one traversal of
    the whole frontier, so a flat score exits after `max_stalls` of them. Every
    number the capped arm asserts is calibrated against this one."""
    logs: list[str] = []
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=logs, batches=batches, cargo_calls=[]), tmp_path,
         monkeypatch, cap="0")
    assert all(len(b) == len(_FRONTIER) for b in batches), (
        "uncapped, every batch is the whole frontier")
    assert len(batches) == 3
    assert any("plateau" in ln for ln in logs)


def test_capped_run_spends_the_same_traversals_not_the_same_iterations(
        tmp_path, monkeypatch) -> None:
    """THE FIX. A cap of 10 over 30 units makes three batches per traversal, so
    a flat score must still buy `max_stalls` TRAVERSALS before the plateau exit
    — nine batches, not three. Pre-fix (stall charged per iteration) this exits
    at three batches, i.e. after ONE traversal, having attempted a third of the
    frontier: the premature-exit bug the cap would otherwise introduce."""
    logs: list[str] = []
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=logs, batches=batches, cargo_calls=[]), tmp_path,
         monkeypatch, cap="10")
    assert all(len(b) == 10 for b in batches), "the cap held every iteration"
    assert len(batches) == 9, (
        f"3 traversals x 3 batches; got {len(batches)} batch(es)")
    # Same work, three times the measurements: one traversal covers the whole
    # frontier, each unit exactly once. (That the batch is a PREFIX of the
    # ranking — so the cap cannot silently reorder dispatch — is pinned in
    # `test_dispatch_batch.py`; the driver's own frontier order comes from set
    # operations upstream of `order_frontier` and is not `_FRONTIER`'s.)
    first_pass = [k for b in batches[:3] for k in b]
    assert sorted(first_pass) == sorted(_FRONTIER)
    assert len(set(first_pass)) == len(_FRONTIER)
    assert any("plateau" in ln for ln in logs)


def test_the_gpu_guard_lets_the_capped_loop_finish_one_traversal(
        tmp_path, monkeypatch) -> None:
    """THE TWO MECHANISMS TOGETHER, which is what a launch runs and what nothing
    covered until this case existed.

    A cap of 10 over 30 units is three batches per traversal. The guard's rule
    is two identical readings, and every reading here is identical — so a guard
    denominated in SWEEPS stops after the FIRST batch, having attempted 10 of
    30, and stops there again on every relaunch (`order_frontier` is
    deterministic and a new process starts with an empty window and an empty
    attempted-set, so the same batch goes out and reads the same score). The
    remaining 20 units are then unreachable without a human editing the env.

    Denominated in TRAVERSALS it lets the loop cover the frontier once and stops
    on the first reading of the second traversal — the fourth batch's sweep. The
    assertion is the batch count: 3 means the whole frontier was dispatched
    exactly once before the run ended."""
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=batches, cargo_calls=[]), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    assert len(batches) == 3, (
        f"the guard must not stop the run mid-traversal; got {len(batches)} "
        "batch(es), so it stopped with part of the frontier never dispatched")
    dispatched = [k for b in batches for k in b]
    assert sorted(dispatched) == sorted(_FRONTIER), \
        "every frontier unit got its turn before the run stopped"


def test_negative_control_a_sweep_denominated_guard_stops_mid_traversal(
        tmp_path, monkeypatch) -> None:
    """The case above with the PRE-FIX rule injected — two identical readings
    stop the run whatever fraction of the frontier they measured.

    It stops after ONE batch of ten, not two: the reading is recorded at the
    sweep and the decision taken before the dispatch, so the second sweep's
    recording is the second occurrence and the run ends before the second batch
    goes out. A THIRD of the frontier attempted, and the same third on every
    relaunch. Without this control the case above asserts a batch count that a
    guard broken some other way could also produce."""
    from pipeline_core.domain.flat_score import FlatScoreGuard

    monkeypatch.setattr(
        FlatScoreGuard, "trips_on",
        lambda self, reading, traversal: (
            self.enabled and self.repeated(reading) >= self.repeats))

    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=batches, cargo_calls=[]), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    assert len(batches) == 1, (
        f"the pre-fix rule stops after one slice; got {len(batches)} — the "
        "control no longer reproduces the defect and the case above proves "
        "nothing")
    assert len({k for b in batches for k in b}) == 10 < len(_FRONTIER)


def test_the_gpu_guard_still_stops_the_capped_loop(tmp_path, monkeypatch) -> None:
    """…and it does STOP: one traversal, not an unbounded number of them. The
    plateau budget is set out of reach (99) so the only terminator available is
    the guard, and the run still ends."""
    logs: list[str] = []
    _run(_deps(logs=logs, batches=[], cargo_calls=[]), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    assert any("GPU_GUARD_STOP" in ln for ln in logs), \
        "a flat capped run with no reachable plateau exit must end by decision"


def test_the_iteration_line_reports_the_batch(tmp_path, monkeypatch) -> None:
    """The cadence has to be readable live — the iteration summary is the one
    line a watcher follows, and the census keys its P7 reaction on this text."""
    logs: list[str] = []
    _run(_deps(logs=logs, batches=[], cargo_calls=[]), tmp_path, monkeypatch,
         cap="10")
    iters = [ln for ln in logs if "cargo test rc=" in ln]
    assert iters and "dispatching 10 of 30" in iters[0]
    assert "mid-pass" in iters[0]


def test_a_graceful_stop_during_the_fill_pass_scores_the_tree_it_leaves(
        tmp_path, monkeypatch) -> None:
    """The hole run `500301-6a66bf86` fell through: it stopped between a fill
    pass and the sweep that would have judged it, so the regression shipped to
    the next run as that run's baseline. The stop must still exit — the sweep
    reports, it does not rescue."""
    logs: list[str] = []
    cargo_calls: list[int] = []

    def _stopping_fill(**kw: Any) -> ClusterFillResult:
        raise SystemExit(0)

    deps = replace(
        _deps(logs=logs, batches=[], cargo_calls=cargo_calls),
        task2_fill_fns_one_by_one=_stopping_fill)
    try:
        _run(deps, tmp_path, monkeypatch, cap="10")
    except SystemExit:
        pass
    else:                                     # pragma: no cover - guard
        raise AssertionError("the graceful stop must still exit")
    final = [ln for ln in logs if "final sweep" in ln]
    assert final, "the tree the run leaves must be measured"
    assert "pass=1" in final[0]
    # One sweep for the iteration, one on the way out.
    assert len(cargo_calls) == 2


def test_a_failing_final_sweep_never_masks_the_stop(
        tmp_path, monkeypatch) -> None:
    """It runs inside the `except` on the way to `raise`. A cargo that blows up
    there must not replace the SystemExit with its own error — that would turn
    a clean stop into a traceback and lose the exit code."""
    logs: list[str] = []
    calls: list[int] = []

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        calls.append(1)
        if len(calls) > 1:
            raise OSError("cargo vanished")
        return 101, _FLAT

    def _stopping_fill(**kw: Any) -> ClusterFillResult:
        raise SystemExit(0)

    deps = replace(
        _deps(logs=logs, batches=[], cargo_calls=[]),
        run_cargo=_cargo, task2_fill_fns_one_by_one=_stopping_fill)
    try:
        _run(deps, tmp_path, monkeypatch, cap="10")
    except SystemExit:
        pass
    else:                                     # pragma: no cover - guard
        raise AssertionError("the graceful stop must still exit")
    assert any("final sweep" in ln and "could not complete" in ln
               for ln in logs)
