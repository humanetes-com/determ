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
from pipeline_core.domain.stub_inventory import StubInventory

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
          cargo_calls: list[int], fill=None, floor: dict | None = None) -> PhaseCTddDeps:
    """`floor`, when given, is a mutable dict standing in for the on-disk
    per-cluster floor file — pass the SAME dict to two `_run` calls to model
    two processes sharing `.phase_c_logs`, which is what the pass cursor is
    persisted for."""
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
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=fill or _fill,
        **({} if floor is None else {
            "load_tdd_floor": lambda *a, **k: floor.get("row"),
            "save_tdd_floor": lambda p, c, row: floor.__setitem__("row", row),
        }),
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


def test_the_gpu_guard_stops_the_capped_loop_on_the_second_repeat(
        tmp_path, monkeypatch) -> None:
    """THE TWO MECHANISMS TOGETHER, which is what a launch runs.

    A cap of 10 over 30 units is three batches per traversal, and every reading
    here is identical. The rule is two identical readings, full stop — so the
    run ends after ONE batch, having attempted 10 of 30. It stops before the
    second batch rather than after it because the reading is recorded at the
    sweep and the decision taken before the dispatch, so the second sweep's
    recording is already the second occurrence.

    Stopping there is the point, not a defect: what makes it safe is that the
    pass cursor is persisted, so the relaunch takes units 11-20 instead of
    re-sending 1-10 (the case below). Before that existed, this eagerness was
    a lock, which is why the traversal precondition was in the rule at all."""
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=batches, cargo_calls=[]), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    assert len(batches) == 1, (
        f"two identical readings must end the run; got {len(batches)} "
        "batch(es), so something let it keep spending")
    assert len({k for b in batches for k in b}) == 10 < len(_FRONTIER)


def test_the_persisted_pass_cursor_makes_the_eager_stop_resumable(
        tmp_path, monkeypatch) -> None:
    """THE REPLACEMENT for the traversal precondition, and the reason removing
    it is not simply a more trigger-happy guard.

    Two processes, one floor file. `order_frontier` is deterministic and both
    runs see the same 30-unit frontier and the same flat readings, so without a
    persisted cursor the second run re-sends the first run's ten units and the
    remaining twenty are unreachable for as many relaunches as anyone cares to
    perform. With it, the second run opens on the units the first never
    reached.

    The assertion is DISJOINTNESS, not a batch count: what matters is that no
    model call is spent twice on the same unit across the stop."""
    floor: dict = {}
    first: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=first, cargo_calls=[], floor=floor),
         tmp_path, monkeypatch, cap="10", guard="2", max_stalls="99")

    second: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=second, cargo_calls=[], floor=floor),
         tmp_path, monkeypatch, cap="10", guard="2", max_stalls="99")

    a = {k for b in first for k in b}
    b = {k for b in second for k in b}
    assert len(a) == 10 and len(b) == 10
    assert not (a & b), (
        "the relaunch re-dispatched units the stopped run had already spent a "
        "call on — the pass cursor did not survive the process boundary, and "
        "the guard's stop is a lock rather than a pause")
    assert (a | b) <= set(_FRONTIER)


def test_the_pass_cursor_survives_an_oracle_size_change_across_the_stop(
        tmp_path, monkeypatch) -> None:
    """The case above with the one transition it avoided: the relaunch's first
    sweep reads a DIFFERENT oracle (a crashed binary flipped — 26 of 401
    consecutive sweep pairs move `oracle_size`, roughly one stop in fifteen).

    `resume_floor` hands the cursor through that branch with `best_pass=None`
    on purpose — a cursor answers to cluster+code_id, not to the oracle. The
    driver then computed `improved` against the UNSET sentinel, read any
    buildable sweep as an improvement, and cleared the cursor it had just
    adopted — so the relaunch re-sent the same deterministic first ten and the
    eager stop was a lock again, with the empty cursor persisted over the
    accumulated one. Same disjointness claim as the same-oracle case; the only
    difference is the second process's cargo output carries five tests where
    the first carried four."""
    wider = ("    Running tests/t.rs (t/t-1)\n"
             "running 5 tests\n"
             "test t1 ... ok\n"
             "test t2 ... FAILED\ntest t3 ... FAILED\n"
             "test t4 ... FAILED\ntest t5 ... FAILED\n"
             "test result: FAILED. 1 passed; 4 failed")
    floor: dict = {}
    first: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=first, cargo_calls=[], floor=floor),
         tmp_path, monkeypatch, cap="10", guard="2", max_stalls="99")

    second: list[tuple[FnLocator, ...]] = []
    deps2 = _deps(logs=[], batches=second, cargo_calls=[], floor=floor)

    def _cargo_wider(*a: Any, **k: Any) -> tuple[int, str]:
        return 101, wider

    _run(replace(deps2, run_cargo=_cargo_wider), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    a = {k for b in first for k in b}
    b = {k for b in second for k in b}
    assert len(a) == 10 and len(b) == 10
    assert not (a & b), (
        "the relaunch re-dispatched units the stopped run had already spent a "
        "call on — the oracle moved, `best_pass` resumed as None, and the "
        "first sweep's vacuous 'improvement' against the UNSET sentinel "
        "cleared the cursor the resume had just handed over")


def test_a_relaunch_crawl_cannot_plateau_close_mid_pass(
        tmp_path, monkeypatch) -> None:
    """THE FIX for the 2026-08-18 close: under the guard's one-batch-per-
    relaunch regime every process stops after its single dispatch, so the
    stall streak was charged at each relaunch's flat first sweep — a sweep
    that measures a MID-PASS batch, exactly what the in-process exemption
    refuses to charge. Three relaunches walked the persisted cursor
    12 -> 24 -> 36 of 43 and the fourth fired `pass_count plateau 3 iter(s)`
    at iter 1, printing `0 of 43 ... dispatched in the stalled window` while
    the floor read 36/43 (runs `3455951-a98b2182`/`3516315-d13ac998`/
    `3579062-476abc3b`) — the first complete escalated pass closed one batch
    short.

    Here: cap 10 over 30 units, guard at 2 identical readings, budget 3.
    Pre-fix the plateau line appears once the cursor has walked ONE
    traversal (three false mid-pass charges); post-fix a resumed non-empty
    cursor seeds `_prev_pass_completed=False`, the exemption restores the
    streak across the process boundary, and the exit needs flat COMPLETED
    traversals — so every unit is dispatched at least twice before the stop
    can fire. The in-process twin of this claim is
    `test_capped_run_spends_the_same_traversals_not_the_same_iterations`."""
    floor: dict = {}
    all_batches: list[tuple[FnLocator, ...]] = []
    plateau_at: int | None = None
    for relaunch in range(1, 13):
        logs: list[str] = []
        batches: list[tuple[FnLocator, ...]] = []
        _run(_deps(logs=logs, batches=batches, cargo_calls=[], floor=floor),
             tmp_path, monkeypatch, cap="10", guard="2", max_stalls="3")
        all_batches.extend(batches)
        if any("plateau" in ln for ln in logs):
            plateau_at = relaunch
            break
    assert plateau_at is not None, (
        "the crawl must still terminate — the exemption may defer the "
        "plateau exit, never delete it")
    counts: dict[FnLocator, int] = {}
    for b in all_batches:
        for k in b:
            counts[k] = counts.get(k, 0) + 1
    assert set(counts) == set(_FRONTIER), (
        "the plateau exit closed the cluster before the first complete pass "
        "— the recorded 2026-08-18 close, one batch short of the cohort")
    assert min(counts.values()) >= 2, (
        f"pre-fix shape: mid-pass relaunch sweeps charged the stall, so the "
        f"exit fired after {min(counts.values())} traversal(s) instead of "
        "flat COMPLETED traversals (unit counts: "
        f"{sorted(counts.values())})")


def test_a_stop_with_a_resumed_cursor_reports_the_record_not_the_process(
        tmp_path, monkeypatch) -> None:
    """The stop line's window accounting. A relaunch that resumes a mid-pass
    cursor, completes the pass with its own batch, and then legitimately
    fires the exit must count the cursor's units as dispatched-in-window
    rather than asserting `0 of N` about a traversal the record shows at
    20/30. The recorded lie: `0 of 43 frontier fn(s) were dispatched in the
    stalled window` printed while the persisted cursor read 36/43."""
    floor: dict = {}
    # Two relaunches walk the cursor to 20 of 30.
    for _ in range(2):
        _run(_deps(logs=[], batches=[], cargo_calls=[], floor=floor),
             tmp_path, monkeypatch, cap="10", guard="2", max_stalls="99")
    # Third relaunch: resumes attempted=20, dispatches the last 10 (pass
    # completes), and its second flat sweep charges the streak. With the
    # budget at 1 the exit fires in THIS process — the one holding both the
    # resumed cursor and its own batch in the window.
    logs: list[str] = []
    _run(_deps(logs=logs, batches=[], cargo_calls=[], floor=floor),
         tmp_path, monkeypatch, cap="10", guard="0", max_stalls="1")
    stop = [ln for ln in logs if "plateau" in ln and "stopping" in ln]
    assert stop, "the exit must fire once the completed pass stays flat"
    assert "30 of 30" in stop[0], (
        f"the stop counted only process-local dispatch: {stop[0]!r} — the "
        "resumed cursor's 20 units were dispatched in this same traversal "
        "and the line must read the record")


def test_negative_control_the_traversal_precondition_reproduces_the_burn(
        tmp_path, monkeypatch) -> None:
    """The case above with the PRE-FIX rule injected — the stop withheld until
    a frontier traversal completes.

    This is the state runs `1064759-0f23fb68` (7h48m), `1692702-94e6163a`
    (3h36m) and `1920662-40f711b2` (9h48m) each burned in, the last of them
    printing `83/106 has been read 2 time(s) (limit 2) but all of them inside
    traversal 0` at 00:25:43Z and running five more hours. Here the loop keeps
    dispatching through the whole frontier instead of stopping at the repeat.
    Without this control the case above asserts a batch count that a guard
    broken some other way could also produce."""
    from pipeline_core.domain.flat_score import FlatScoreGuard

    monkeypatch.setenv("TDD_FLAT_SCORE_REQUIRE_TRAVERSAL", "1")
    assert FlatScoreGuard(require_traversal=True).repeats == 2

    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=[], batches=batches, cargo_calls=[]), tmp_path, monkeypatch,
         cap="10", guard="2", max_stalls="99")

    assert len(batches) >= 3, (
        f"the pre-fix rule spends the whole traversal; got {len(batches)} — "
        "the control no longer reproduces the burn and the case above proves "
        "nothing")
    assert {k for b in batches[:3] for k in b} == set(_FRONTIER)


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
