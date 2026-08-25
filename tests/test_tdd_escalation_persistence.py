"""The futility evidence survives the process boundary, so a resumed flat run
can escalate — and dispatch the escalated slice — at its FIRST sweep.

The lock this pins, measured on run `2773540-e47c3754` (2026-08-17, 105m20s,
exit 0): the futility escalation's trigger is "this test's attributed unit-set
is UNCHANGED across two judged sweeps", held in a process-local dict — so it
can fire no earlier than a process's second sweep. The flat-score guard's
repeat rule stops a resumed flat run at exactly that sweep, BEFORE the widened
frontier's first dispatch. And the escalation's frontier additions are
in-memory, so they die with the process and the next relaunch opens on the
consumers-only frontier again. Net: the escalated producer/traversal-step
batch — which on the live tree held `context.rs::source`, still returning the
`None` the 2026-08-14 diagnosis named — was computed on the sweep the stop
pre-empted, in every process, forever. The guard's own design doc promises "a
relaunch dispatches a slice the previous process never reached"; that was true
of the consumer frontier only.

The fix: `TddFloor` persists `prev_test_units` (per failing test, the last
sweep's attributed unit keys), resumed on the CURSOR's identity rule
(cluster + code_id — the evidence self-validates at the live sweep via the
escalation's own equality re-check). Seeded, the resume's first sweep is the
second observation, the escalation fires before the one batch the guard's
design grants each relaunch, and the resumed pass cursor steers that batch
onto the units the stopped process never attempted.
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

_ACC = FnLocator("src/a.rs", "fmt")        # the futile accessor (consumer)
_PROD = FnLocator("src/b.rs", "producer")  # the escalation's addition

_FLAT = ("    Running tests/t.rs (t/t-1)\n"
         "running 4 tests\n"
         "test t1 ... ok\n"
         "test t2 ... FAILED\ntest t3 ... FAILED\ntest t4 ... FAILED\n"
         "test result: FAILED. 1 passed; 3 failed")


def _deps(*, logs: list[str], events: list[tuple[str, dict]],
          batches: list[tuple[FnLocator, ...]], floor: dict,
          escalations: list[tuple[str, frozenset[FnLocator]]],
          suspensions: list[tuple[frozenset[FnLocator], frozenset[str]]]
          | None = None) -> PhaseCTddDeps:
    """One process's deps. Pass the SAME `floor` dict to two calls to model
    two processes sharing the on-disk `.phase_c_logs` floor file."""
    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        return 101, _FLAT

    def _fill(**kw: Any) -> ClusterFillResult:
        batches.append(tuple(kw.get("tdd_frontier_keys") or ()))
        return ClusterFillResult(attempts=1)

    def _escalate(src_dir: Path, tname: str, units: set[FnLocator],
                  producers_out=None):
        escalations.append((tname, frozenset(units)))
        return {_PROD: {tname}}

    def _suspend(pd: Path, sd: Path, units, cl: str, tests) -> list[str]:
        if suspensions is not None:
            suspensions.append((frozenset(units), frozenset(tests)))
        return ["src/a.rs::fmt::impl Fmt"]

    return PhaseCTddDeps(
        attribute_failures=lambda *a, **k: AttributionResult(
            {_ACC: {"t2"}}, FailureKind.RUNTIME, {_ACC: {"t2"}}),
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src: frontier,
        check_graceful_stop=lambda *a, **k: None,
        check_auto_operator=lambda *a, **k: None,
        converge_in_cluster=lambda *a, **k: None,
        count_relevant_notes=lambda *a, **k: 0,
        effective_stalls=lambda budget, module: budget,
        emit=lambda ev, **k: events.append((ev, k.get("data") or {})),
        log=logs.append,
        order_frontier=lambda eligible, counts: list(eligible.items()),
        record_tdd_regression=lambda *a, **k: "",
        reset_exhaustion_flags=lambda *a, **k: [],
        run_cargo=_cargo,
        scan_unimplemented_for_cluster=lambda *a, **k: [],
        scan_unimplemented_functions=lambda *a, **k: [],
        task2_fill_fns_one_by_one=_fill,
        escalate_to_producers=_escalate,
        suspend_futile_guidance=_suspend,
        load_tdd_floor=lambda *a, **k: floor.get("row"),
        save_tdd_floor=lambda p, c, row: floor.__setitem__("row", row),
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch, *,
         guard: str = "2") -> None:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    # High enough that the plateau budget never terminates these two-sweep
    # processes: the terminator under test is the flat-score guard, exactly as
    # in the recorded run (whose stall stood at 1/3 when the guard stopped it).
    monkeypatch.setenv("TDD_MAX_STALLS", "9")
    # The guard is ON: the lock under test is the interaction between its
    # repeat rule and the escalation's two-sweep trigger.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", guard)
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", "0")
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]")


def _stopped_process(tmp_path, monkeypatch, floor: dict) -> tuple[
        list[str], list[tuple[FnLocator, ...]],
        list[tuple[str, frozenset[FnLocator]]]]:
    """Process 1: a flat run under the guard. Its second sweep escalates AND
    trips the stop, so the escalated batch is never dispatched — the recorded
    shape of run `2773540-e47c3754`, reproduced here as the setup every case
    below resumes from."""
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    return logs, batches, escalations


def test_the_stopped_process_computes_the_escalation_and_never_dispatches_it(
        tmp_path, monkeypatch) -> None:
    """The lock's first half, in one process: escalation fires at sweep 2,
    the guard stops the run at the same sweep, the producer reaches no batch —
    and the floor left on disk carries the futility evidence."""
    floor: dict = {}
    logs, batches, escalations = _stopped_process(tmp_path, monkeypatch, floor)
    assert escalations and escalations[0][0] == "t2"
    assert any("GPU_GUARD_STOP" in ln for ln in logs)
    assert batches, "the first sweep's batch went out"
    assert all(_PROD not in b for b in batches), (
        "the escalated producer was computed and never dispatched — the lock")
    row = floor.get("row") or {}
    assert row.get("futile_units") == {"t2": ["src/a.rs::fmt"]}, (
        "the stopped process's floor must carry the evidence the resume needs")


def test_a_resumed_process_escalates_at_its_first_sweep_and_dispatches_the_producer(
        tmp_path, monkeypatch) -> None:
    """THE FIX. The resume seeds `prev_test_units` from the floor, so its
    first sweep is the second observation: the escalation fires at iter 1,
    and the one batch the guard grants this process is spent on the escalated
    slice (the resumed cursor already excludes the attempted accessor)."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)

    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    assert escalations and escalations[0] == ("t2", frozenset({_ACC})), (
        "escalation must fire from the persisted evidence")
    # It fired BEFORE the first dispatch: iter 1's batch carries the addition.
    assert batches and _PROD in batches[0], (
        f"iter 1's batch must hold the escalated producer, got {batches[0]}")
    assert any("futility evidence" in ln for ln in logs), (
        "the floor line must say the evidence was resumed")


def test_without_the_persisted_evidence_the_lock_reproduces(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL — a floor written by the pre-fix build (no
    `futile_units` key) resumes cursor and scores but seeds nothing, so the
    resumed process re-runs the recorded lock: escalation waits for its own
    second sweep, the guard stops that sweep before dispatch, and the
    producer again reaches no batch."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row.pop("futile_units", None)   # what an old build persisted
    floor["row"] = row

    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    assert any("GPU_GUARD_STOP" in ln for ln in logs)
    assert all(_PROD not in b for b in batches), (
        "pre-fix shape: the escalated producer must again reach no batch")


def test_a_fired_escalation_suspends_the_futile_units_stored_guidance(
        tmp_path, monkeypatch) -> None:
    """187.2's driver seam: when the escalation fires, the futile accessors'
    retro guidance is handed to the suspension hook — the record advises the
    exact rewrite the escalation just measured as changing nothing."""
    floor: dict = {}
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    suspensions: list[tuple[frozenset[FnLocator], frozenset[str]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations, suspensions=suspensions),
         tmp_path, monkeypatch)
    assert suspensions == [(frozenset({_ACC}), frozenset({"t2"}))]
    assert any("suspended stored guidance" in ln for ln in logs)


def test_futility_survives_a_code_move(tmp_path, monkeypatch) -> None:
    """THE PLAN step 4 (202.5's bar, reachability only). The grade describes
    the TREE, so a `code_id` move — the shape of every validation launch —
    no longer voids the evidence: the resumed process escalates at its FIRST
    sweep and iter 1's batch carries the producer instead of re-buying the
    accessor traversal (the toll runs `21368-93f6a589` → `40938-f4e8e94b`
    paid twice in one evening on the same git tree, and the three runs of
    2026-08-25 paid it once each — 2h03m/2h18m/4h02m)."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row["code_id"] = "a-different-build"   # the validation-launch shape
    floor["row"] = row

    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    assert escalations and escalations[0] == ("t2", frozenset({_ACC})), (
        "escalation must fire from the resumed evidence across the code move")
    assert batches and _PROD in batches[0], (
        f"iter 1's batch must hold the escalated producer, got {batches[0]}")
    assert any("across the code move" in ln for ln in logs)


def test_a_resumed_process_re_saves_the_seed_it_did_not_re_measure(
        tmp_path, monkeypatch) -> None:
    """The seed cannot be erased by a relaunch that never re-measures it.
    A stopped process escalates and re-persists `prev_test_units` from the
    resumed evidence on its own first boundary; without this, run
    `4911-9ab8852e` (2026-08-25) saved an empty `futile_units: {}` one sweep
    into a fresh resume and blanked the seed for every later relaunch — the
    file-fingerprint premise had refused the carry, so nothing seeded
    `prev_test_units` and the boundary save wrote it empty."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row["code_id"] = "a-different-build"
    floor["row"] = row

    _run(_deps(logs=[], events=[], batches=[], floor=floor,
               escalations=[]), tmp_path, monkeypatch)
    assert (floor["row"].get("futile_units") or {}).get("t2") == [
        "src/a.rs::fmt"], (
        "the resumed process must re-persist the evidence, not blank it")
