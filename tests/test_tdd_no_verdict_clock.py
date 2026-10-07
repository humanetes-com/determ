"""The GPU protector's SECOND clock, inside the TDD driver.

THE INSTANCE. Run `1692702-94e6163a` (2026-08-16) took three judged sweeps of
cluster `stable` reading `pass=79 oracle=101`, landed 18 and 28 bodies between
them with a byte-identical per-binary vector, and could not stop itself. Both
existing terminators are denominated in COMPLETED FRONTIER TRAVERSALS — the
flat-score guard needs `first_traversal < traversal`, the plateau budget charges
only after a completed pass — and the frontier grew 35 -> 57 -> 63 against a
cursor at 12 -> 21 -> 36, so no pass ever closed. `_traversals` stayed 0, the
guard's test read `0 < 0`, `stall` read 0 of 3, and 3h36m and 84 model calls
went by with no exit the loop could take.

The cohort fix in `domain/dispatch_batch.py` makes traversals tick again. This
clock is what makes the stop independent of whether they do: it counts judged
sweeps that landed bodies and moved no test's verdict, which no frontier
bookkeeping can disable.

Every case states its negative control: with `TDD_NO_VERDICT_SWEEPS=0` the
recorded non-terminating behaviour reproduces.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain import graceful_stop
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import BuildCluster, ClusterFillResult, FnLocator
from pipeline_core.domain.stub_inventory import StubInventory


def _sweep(passed: int, failed: int, *, binary: str = "tests/t.rs") -> str:
    """A cargo sweep whose per-binary vector is derivable — the axis this clock
    reads. Stable failing names keep the frontier identical every iteration."""
    lines = [f"     Running {binary} (target/debug/deps/t-1)"]
    lines += [f"test p{i} ... ok" for i in range(passed)]
    lines += [f"test t{i} ... FAILED" for i in range(failed)]
    lines.append(f"test result: FAILED. {passed} passed; {failed} failed")
    return "\n".join(lines)


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


@pytest.fixture(autouse=True)
def _clear_stop() -> Any:
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


def _run(tmp_path: Path, monkeypatch, sweeps: list[str],
         events: list[tuple[str, dict]], *, limit: str = "3",
         logs: list[str] | None = None, targets: int = 60,
         fills: int = 1, stalls: str = "6", flat_repeats: str = "0") -> None:
    """Drive the loop over `sweeps` (the last repeats forever).

    The defaults reproduce the recorded position: a 60-unit frontier against
    the production dispatch cap of 12, so a traversal is five iterations and
    the clock's third sweep arrives while `traversals` is still 0 — which is
    the state that disabled both traversal-denominated terminators live. The
    plateau budget is loose enough not to decide these cases first, and fills
    LAND (reporting zero fills would leave every sweep unjudged and prove
    nothing)."""
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", stalls)
    # The FIRST clock off: on this fixture it cannot fire anyway (no pass
    # closes, which is the point), and leaving it armed would make a passing
    # case ambiguous about which terminator ended the run.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", flat_repeats)
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", limit)
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    seq = iter(sweeps)
    last = [sweeps[0]]

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        last[0] = next(seq, last[0])
        return (101, last[0])

    _front = {FnLocator("src/a.rs", f"producer{i}"): {"t0"}
              for i in range(targets)}
    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            dict(_front), FailureKind.RUNTIME, dict(_front)),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=(logs.append if logs is not None else (lambda *a, **k: None)),
        order_frontier=lambda eligible, counts_: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(
            attempts=1, fns_filled=fills, bytes_filled=100 * fills),
    )
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t0() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def _kinds(events: list[tuple[str, dict]]) -> list[str]:
    return [d.get("kind") for ev, d in events if ev == "phase_c.tdd.iter"]


def test_three_unmoved_sweeps_stop_the_run_without_a_completed_traversal(
        tmp_path: Path, monkeypatch) -> None:
    """The recorded position, with the fix: bodies land, the vector does not
    move, and the run ends on its own at the third judged sweep — while
    `traversals` is still 0, which is exactly the number that disabled every
    other terminator."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events)

    stops = [d for ev, d in events if ev == "phase_c.tdd.no_verdict_stop"]
    assert len(stops) == 1
    assert stops[0]["streak"] == 3 and stops[0]["limit"] == 3
    assert stops[0]["landed"] >= 3
    assert stops[0]["traversals"] == 0, \
        "the stop must not depend on a traversal — that is the whole point"
    assert stops[0]["pass_count"] == 79 and stops[0]["oracle_size"] == 101
    assert "no_verdict_stop" in _kinds(events)
    assert graceful_stop._STOP_EVENT.is_set(), \
        "the stop has to be the process-wide one every worker pool honours"


def test_with_the_clock_off_the_run_keeps_spending(
        tmp_path: Path, monkeypatch) -> None:
    """THE NEGATIVE CONTROL. Same fixture, `TDD_NO_VERDICT_SWEEPS=0`: nothing
    stops at the third unmoved sweep and the loop keeps dispatching against a
    vector that has not moved, until a budget denominated in something else
    happens to end it. Live, where that budget was also traversal-denominated,
    "something else" was an operator."""
    off: list[tuple[str, dict]] = []
    on: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], off, limit="0")
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], on, limit="3")

    assert not [d for ev, d in off if ev == "phase_c.tdd.no_verdict_stop"]
    assert "no_verdict_stop" not in _kinds(off)
    assert len(_kinds(off)) > len(_kinds(on)), \
        "the clock has to shorten the run it was written for"


def test_a_moved_verdict_restarts_the_streak(
        tmp_path: Path, monkeypatch) -> None:
    """The clock measures "nothing changed", not "the score is low". A sweep
    whose per-binary vector moves — in either direction — is the loop
    demonstrably doing something, so the count restarts and the two unmoved
    sweeps around it never add up to a stop."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch,
         [_sweep(79, 22), _sweep(79, 22), _sweep(80, 21), _sweep(80, 21)],
         events, limit="3")

    stops = [d for ev, d in events if ev == "phase_c.tdd.no_verdict_stop"]
    # The last sweep repeats forever, so a stop is eventually correct — what is
    # pinned is that it did NOT come from the two sweeps before the move.
    assert not stops or stops[0]["iter"] > 4


def test_a_sweep_that_landed_nothing_is_not_counted(
        tmp_path: Path, monkeypatch) -> None:
    """A sweep with no fills between it and the last one judges no dispatch —
    it says nothing in either direction, so it must neither count nor reset.
    Reporting zero fills is how the loop looks when it is idle, and stopping a
    run for being idle would be a different (and wrong) claim."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events, fills=0)

    assert not [d for ev, d in events if ev == "phase_c.tdd.no_verdict_stop"]


def test_the_withheld_flat_guard_reports_itself_every_sweep(
        tmp_path: Path, monkeypatch) -> None:
    """The guard is silent when it does not trip, so "armed but unable to
    charge" and "making progress" looked identical in the log for 3h36m. With
    the repeat clause satisfied and the traversal clause not, every sweep says
    so — and names the cohort progress that is the only thing that can release
    it.

    The traversal clause is OFF by default since 2026-08-17, so nothing is
    withheld on a default run and this report is vacuous there. It is driven
    here through `TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1`, which is the whole
    reason `withheld_by_traversal` survived the change rather than being
    deleted: an operator who re-arms the precondition gets back the line that
    says the precondition is what is holding the stop."""
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    # The guard ARMED (repeats=2) and the second clock OFF, so the run survives
    # long enough to print the line and nothing else ends it first.
    monkeypatch.setenv("TDD_FLAT_SCORE_REQUIRE_TRAVERSAL", "1")
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events, limit="0",
         flat_repeats="2", logs=logs)

    withheld = [d for ev, d in events if ev == "phase_c.tdd.flat_score_withheld"]
    assert withheld, "the armed-and-withheld state printed nothing"
    assert withheld[0]["traversals"] == 0
    assert withheld[0]["repeats"] >= 2
    assert withheld[0]["cohort_size"] > 0
    assert any("ARMED and WITHHELD" in ln for ln in logs)


def test_the_clock_and_the_guard_share_one_stop_site(
        tmp_path: Path, monkeypatch) -> None:
    """Both clocks route through the durable-tree rescue: a second exit that
    skipped it would be a second way to end a run on a tree worse than one
    already on disk. Here the mark is better than the sweep, so the clock's
    trip restores instead of stopping."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    events: list[tuple[str, dict]] = []
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    # A layout the live tree is not: the entry restore declines it, and the
    # restore read here is the one before the stop.
    _better = BestTreeMark(pass_count=95, iteration=1,
                           tree_sha="a" * 16, oracle_size=101,
                           layout_sha="f" * 16)

    seq = iter([_sweep(79, 22)])
    last = [_sweep(79, 22)]

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        last[0] = next(seq, last[0])
        return (101, last[0])

    _front = {FnLocator("src/a.rs", f"p{i}"): {"t0"} for i in range(60)}
    restored: list[int] = []
    monkeypatch.setenv("TDD_MAX_STALLS", "99")
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", "3")
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            dict(_front), FailureKind.RUNTIME, dict(_front)),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=lambda *a, **k: None,
        order_frontier=lambda eligible, counts_: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=lambda **kw: ClusterFillResult(
            attempts=1, fns_filled=1, bytes_filled=100),
        load_best_tree_mark=lambda *a, **k: _better,
        restore_best_tree_pair=lambda *a, **k: (
            restored.append(1) or (9, 12, [])),
    )
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t0() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")

    assert restored, "the clock stopped without offering the better tree"
    assert [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
