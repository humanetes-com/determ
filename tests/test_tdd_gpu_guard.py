"""The GPU protector inside the TDD driver: the run stops before the dispatch
that would spend model calls on a score already read.

The instance. Run `1064759-0f23fb68` ran 2026-08-15 20:08 → 2026-08-16 04:56 UTC
(7h48m) and took seven scored sweeps of cluster `stable`; six read
`pass=79 fail=22 oracle=101` with byte-identical per-binary vectors, so ~70
landed fills changed no test's verdict. The plateau budget stood at `stall=1/3`
when an operator's SIGTERM ended it — it charges only on a COMPLETED frontier
traversal, and at a dispatch cap of 12 over a 59-fn frontier a traversal is five
iterations. The loop had no terminator that could fire in a night.

Each test states its negative control: with the guard off, the recorded
behaviour reproduces.
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

_TARGET = FnLocator("src/a.rs", "producer")


def _sweep(passed: int, failed: int) -> str:
    """A cargo sweep reading `passed`/`passed+failed`, with the failing names
    stable so the frontier is the same set every iteration."""
    lines = [f"test p{i} ... ok" for i in range(passed)]
    lines += [f"test t{i} ... FAILED" for i in range(failed)]
    lines.append(f"test result: FAILED. {passed} passed; {failed} failed")
    return "\n".join(lines)


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


@pytest.fixture(autouse=True)
def _clear_stop() -> Any:
    """The stop event is process-wide state — a test that trips the guard would
    otherwise stop every test after it."""
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


def _run(tmp_path: Path, monkeypatch, sweeps: list[str],
         events: list[tuple[str, dict]], *, stubs: list[int] | None = None,
         guard: str = "2", stalls: str = "9", targets: int = 1,
         refill_max: str = "",
         best_mark: Any = None, restore: Any = None,
         fill_result: Any = None) -> ClusterFillResult:
    """Drive the loop over `sweeps` (the last repeats forever) with a stub count
    following `stubs` (default: flat, i.e. pure behavioural re-fill — every
    dispatched unit in the recorded run logged `0 stub(s)`).

    `stalls` is deliberately loose by default: the plateau exit is checked
    EARLIER in the iteration than the guard, so a tight budget would decide
    these cases before the guard is reached and prove nothing about it. Live the
    order is the other way round — the budget charges only on a completed
    traversal, which under the dispatch cap is ~5 iterations.

    `targets` widens the BEHAVIOURAL frontier (`producer0…`), which is what the
    live tree's frontier is made of — run `500301-6a66bf86`'s 61 entries were
    all re-fills, its 14 remaining stubs all cfg-inactive. Stub targets drain:
    a first-fill attempt that leaves the fn a stub exhausts it immediately, so
    a stub fixture cannot hold a frontier still across the traversal these
    cases now need. `refill_max` keeps the behavioural ones admitted.

    THE DISPATCH CAP IS LEFT AT ITS PRODUCTION DEFAULT here, deliberately. It is
    the configuration a launch actually runs, and the review that added the
    traversal clause found that no test and no walk exercised it together with
    this guard: the cadence tests set the guard to 0 and every walk drives a
    one-fn frontier."""
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    monkeypatch.setenv("TDD_MAX_STALLS", stalls)
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", guard)
    # Only when a case asks: raising it keeps a behavioural frontier admitted
    # across a traversal, and leaving it raised everywhere would silently let
    # every other case run past the sweeps it scripted.
    if refill_max:
        monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", refill_max)
    seq = iter(sweeps)
    last = [sweeps[0]]
    stub_seq = iter(stubs or [4])
    stub_last = [(stubs or [4])[0]]

    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        last[0] = next(seq, last[0])
        return (101, last[0])

    def _scan(*a: Any, **k: Any) -> list[FnLocator]:
        stub_last[0] = next(stub_seq, stub_last[0])
        return [FnLocator("src/a.rs", f"stub{i}") for i in range(stub_last[0])]

    _front = {FnLocator("src/a.rs", f"producer{i}"): {"t0"}
              for i in range(targets)} if targets > 1 else {_TARGET: {"t0"}}

    deps = PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            dict(_front), FailureKind.RUNTIME, dict(_front)),
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
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=_scan,
        scan_unimplemented_functions=lambda *a, **k: [],
        # Fills LAND and buy nothing — the recorded shape. Reporting zero fills
        # instead would let the driver's re-entry filter retire the frontier
        # after two dispatches, and the loop would idle rather than spend.
        task2_fill_fns_one_by_one=lambda **kw: (
            fill_result if fill_result is not None
            else ClusterFillResult(attempts=1, fns_filled=1, bytes_filled=100)),
        load_best_tree_mark=(lambda *a, **k: best_mark) if best_mark else None,
        restore_best_tree_pair=restore,
    )
    return run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t0() {}")], module="m",
        cluster=_cluster(), check_cmd="cargo check", cluster_env={},
        leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def _mark(pass_count: int, oracle_size: int) -> Any:
    """A durable best-tree mark, as `load_best_tree_mark` returns one."""
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    return BestTreeMark(pass_count=pass_count, iteration=1,
                        tree_sha="a" * 16, oracle_size=oracle_size)


def _iters(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.iter"]


def test_the_run_stops_at_the_second_reading_of_the_same_score(
        tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events)

    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert len(stops) == 1, "the guard fires once and ends the run"
    assert stops[0]["pass_count"] == 79 and stops[0]["oracle_size"] == 101
    assert stops[0]["repeats"] == 2 and stops[0]["iter"] == 2
    assert _iters(events)[-1]["kind"] == "flat_score_stop"
    # And the run stops, not just the cluster: the process-wide event every
    # worker pool and phase boundary already checks is set.
    assert graceful_stop._STOP_EVENT.is_set()


def test_negative_control_the_recorded_burn_reproduces_with_the_guard_off(
        tmp_path: Path, monkeypatch) -> None:
    """Pre-fix behaviour: the identical sweep sequence runs on until the plateau
    budget closes it — three charges, and live those were 5 iterations apart."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events, guard="0",
         stalls="3")

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.flat_score_stop"]
    assert not graceful_stop._STOP_EVENT.is_set()
    iters = _iters(events)
    assert iters[-1]["kind"] == "stall_exit"
    assert len(iters) > 2, "the pre-fix loop keeps dispatching"


def test_nothing_is_convicted_on_the_way_out(
        tmp_path: Path, monkeypatch) -> None:
    """The guard judges the SPEND, not the units. A conviction written here
    would persist to disk and travel to the next run as evidence no measurement
    produced — the stall exit's verdicts are earned by a completed traversal."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events)

    verdicts = [d for ev, d in events if ev == "phase_c.verdict"]
    assert not [v for v in verdicts if v.get("disposition") == "exhaustion"]


def test_a_climbing_score_is_never_stopped(tmp_path: Path, monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch,
         [_sweep(70, 31), _sweep(72, 29), _sweep(74, 27), _sweep(76, 25),
          _sweep(78, 23), _sweep(80, 21)], events)

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.flat_score_stop"]


def test_a_reading_returned_to_after_a_detour_still_stops(
        tmp_path: Path, monkeypatch) -> None:
    """The window, not a consecutive streak: run `1064759-0f23fb68` read 79,
    then 66 on the sweep after a 1-body re-stub, then 79 again."""
    events: list[tuple[str, dict]] = []
    # A frontier wider than the dispatch cap, so the third sweep still has
    # something to spend on — as the live 59-fn frontier did at a cap of 12.
    _run(tmp_path, monkeypatch,
         [_sweep(79, 22), _sweep(66, 35), _sweep(79, 22)], events, stubs=[40])

    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert len(stops) == 1
    assert stops[0]["pass_count"] == 79
    # Each reading carries the traversal it was taken in. 41 units at a cap of
    # 12 is four iterations, so the first four sit in traversal 0 and the stop
    # is taken on the first reading of traversal 1 — the detour is inside the
    # window, and the repeat that ends the run is the one that survived the
    # loop finishing its plan.
    assert stops[0]["readings"] == [[79, 101, 0], [66, 101, 0], [79, 101, 0],
                                    [79, 101, 0], [79, 101, 1]]
    assert stops[0]["traversals"] == 1 and stops[0]["iter"] == 5


def test_a_shrinking_stub_set_is_not_judged(tmp_path: Path, monkeypatch) -> None:
    """The negative control that costs a run if it fails. Cluster `stable` of
    run `7445-00ee8a3c` (2026-08-07) went 11 → 13 → 13 on the pass count while
    the stub set fell 132 → 118 → 82 → 75; iteration 3's file-first batch then
    took the score 13 → 83 in one sweep. A guard that stops a cluster paying in
    stubs stops it one iteration before it is paid back."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch,
         [_sweep(11, 90), _sweep(13, 88), _sweep(13, 88), _sweep(83, 18)],
         events, stubs=[132, 118, 82, 75, 70])

    reached = [d["pass_count"] for d in _iters(events)]
    assert 83 in reached, \
        f"the flat 13 -> 13 must not stop a cluster one sweep before its "\
        f"stub-paid fills land; readings were {reached}"
    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert all(d["pass_count"] == 83 for d in stops), \
        "any stop must be about the fixed point AFTER the climb, not during it"


def test_a_flat_score_over_a_flat_stub_set_still_stops(
        tmp_path: Path, monkeypatch) -> None:
    """The exemption is bought with stubs, not with activity: once the stub set
    stops falling, the same reading stops the run.

    A 119-unit frontier at a cap of 12 is a ten-iteration traversal, and the
    stop is taken on the sweep after it closes. That is the price of the
    traversal clause, stated as a number rather than left implicit: the guard
    cannot end a run before the loop has been round its own plan once, because
    until then "the calls about to be spent are the ones that did not move it"
    is not true of the units about to go out."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(13, 88)], events,
         stubs=[132, 118, 118, 118])

    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert len(stops) == 1 and stops[0]["pass_count"] == 13
    assert stops[0]["traversals"] == 1
    assert stops[0]["iter"] == 11, \
        "119 units at a cap of 12 is ten dispatching iterations, then the stop"
    assert all(r[:2] == [13, 101] for r in stops[0]["readings"]), \
        "the sweep that shrank the stub set is excused, so it is not a reading"


def test_the_excused_sweep_is_reported_as_the_gap_it_was(
        tmp_path: Path, monkeypatch) -> None:
    """A stub-shrinking sweep leaves the window untouched and is COUNTED, so the
    stop line can say how far apart the two identical readings really were —
    the one thing an operator needs to judge the stop by hand. Frontier under
    the cap here, so a traversal is one iteration and the gap is still adjacent
    to the trip when it is reported. The behavioural targets are what keep a
    frontier alive at all once the stubs have been dispatched and exhausted."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(13, 88)], events, stubs=[6, 3, 3, 3],
         targets=3, refill_max="99")

    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert len(stops) == 1 and stops[0]["skipped"] == 1


def test_the_same_pass_count_at_a_different_oracle_is_not_a_repeat(
        tmp_path: Path, monkeypatch) -> None:
    """Eight of the twelve sweeps before this run read `39`, at oracles 79, 87,
    101, 103 and 105. Two of those readings are not the same measurement."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch,
         [_sweep(39, 48), _sweep(39, 40), _sweep(39, 64)], events)

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.flat_score_stop"]


def test_a_better_tree_on_disk_is_restored_instead_of_stopping(
        tmp_path: Path, monkeypatch) -> None:
    """The guard's first question is not "should I stop" but "is there a better
    tree on disk that this run has not tried". Measured over runs
    `1082156-4da4faf7` → `669347-cf33eae3` (2026-08-14/15): a retrigger traded
    a reproducible 79/101 for two tests that have never passed in any run,
    eight of the next twelve scored sweeps read exactly 39, and no mechanism in
    the loop could take the 79 back."""
    events: list[tuple[str, dict]] = []
    restored: list[tuple] = []

    _Mark = _mark(79, 101)

    def _restore(pd, cl, filled):
        restored.append((pd, cl, filled))
        return (9, 12, ["src/new.rs"])

    # A frontier wider than the dispatch cap, so the sweeps after the restore
    # still have something to spend and the guard is reached again.
    _run(tmp_path, monkeypatch, [_sweep(39, 62)], events, stubs=[40],
         best_mark=_Mark, restore=_restore)

    rows = [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
    assert len(rows) == 1, "restore once per process, never a loop"
    assert rows[0]["mark_pass"] == 79 and rows[0]["pass_count"] == 39
    assert rows[0]["bodies"] == 9 and rows[0]["layout_files"] == 12
    assert rows[0]["leftovers"] == ["src/new.rs"]
    assert restored and restored[0][1] == "stable"
    # And the run does not end on that sweep: the restore IS the change the stop
    # would otherwise have asked a human for, so the loop goes on with it.
    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert not [d for d in stops if d["iter"] == rows[0]["iter"]]
    assert [d for d in _iters(events) if d["iter"] > rows[0]["iter"]], \
        "the loop keeps going on the restored tree"


def test_a_restored_tree_that_also_goes_flat_stops_the_run(
        tmp_path: Path, monkeypatch) -> None:
    """The restore is spent once per process and the guard behind it still
    ends the run — with the better tree in place, which is the better place to
    stop. A behavioural frontier here rather than a stub one: a stub that a
    first-fill attempt leaves stubbed is exhausted on the spot, so a stub
    fixture drains its own frontier before a second traversal can happen and
    the guard is never reached again."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(39, 62)], events, targets=30,
         stubs=[0], refill_max="99", best_mark=_mark(79, 101),
         restore=lambda pd, cl, f: (9, 12, []))

    rows = [d for ev, d in events if ev == "phase_c.tdd.best_tree_restored"]
    stops = [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]
    assert len(rows) == 1 and len(stops) == 1
    assert stops[0]["iter"] > rows[0]["iter"]
    # The restore cleared the window, so every reading behind the stop was
    # taken after it — none of them describes a tree that is gone. The window
    # holds at most `window` of them and the restore happened at iter
    # rows[0]["iter"], so the count is the proof: a window still carrying
    # pre-restore readings would be longer than the sweeps since.
    assert len(stops[0]["readings"]) <= stops[0]["iter"] - rows[0]["iter"]


def test_a_slot_without_the_layout_half_is_declined_and_the_run_stops(
        tmp_path: Path, monkeypatch) -> None:
    """Bodies from one run do not compile against another's skeleton — t1.5
    re-authors it every run. A restore that cannot supply the PAIR would be a
    second destruction, so the loop declines it and takes the stop."""
    events: list[tuple[str, dict]] = []

    _Mark = _mark(79, 101)

    _run(tmp_path, monkeypatch, [_sweep(39, 62)], events,
         best_mark=_Mark, restore=lambda *a: None)

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.best_tree_restored"]
    assert [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]


def test_a_mark_no_better_than_the_tree_is_not_restored(
        tmp_path: Path, monkeypatch) -> None:
    """A restore is only ever a recovery. Putting back a tree that scores what
    this one scores discards the run's fills for nothing."""
    events: list[tuple[str, dict]] = []

    _Mark = _mark(39, 62)

    _run(tmp_path, monkeypatch, [_sweep(39, 62)], events,
         best_mark=_Mark, restore=lambda *a: (1, 1, []))

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.best_tree_restored"]
    assert [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]


def test_the_no_verdict_moved_contradiction_is_printed(
        tmp_path: Path, monkeypatch) -> None:
    """31 positive unit verdicts in a sweep with zero delta, and nothing
    printed the contradiction — filed 2026-08-14, reproduced at 50 on run
    `1064759-0f23fb68`. The two numbers are in hand on the same line."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events, stubs=[40],
         guard="0", stalls="4")

    rows = [d for ev, d in events if ev == "phase_c.tdd.no_verdict_moved"]
    assert rows, "a fill pass that moved no verdict must say so"
    assert rows[0]["filled_since"] >= 1 and rows[0]["pass_count"] == 79


def test_no_contradiction_line_when_nothing_landed(
        tmp_path: Path, monkeypatch) -> None:
    """An idle sweep is not a contradiction — the line has to mean that fills
    landed and bought nothing, or it becomes noise the reader learns to skip."""
    events: list[tuple[str, dict]] = []
    _run(tmp_path, monkeypatch, [_sweep(79, 22)], events, guard="0", stalls="4",
         fill_result=ClusterFillResult(attempts=1))

    assert not [ev for ev, _ in events if ev == "phase_c.tdd.no_verdict_moved"]
