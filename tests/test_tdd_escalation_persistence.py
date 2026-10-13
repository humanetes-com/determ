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

import pytest

from pipeline_core.application.reconstruct.phase_c_tdd import (
    PhaseCTddDeps,
    run_phase_c_tdd,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.body_ledger import BodyState, LedgerEntry, LedgerKey
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    TypeRecord,
)
from pipeline_core.domain.stub_inventory import StubInventory

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
          | None = None,
          fill=None, attribute=None, run_id: str = "",
          ledger_load=None, grades: list[frozenset[FnLocator]] | None = None,
          enumerate_fns=None, enumerate_types=None) -> PhaseCTddDeps:
    """One process's deps. Pass the SAME `floor` dict to two calls to model
    two processes sharing the on-disk `.phase_c_logs` floor file."""
    def _cargo(*a: Any, **k: Any) -> tuple[int, str]:
        return 101, _FLAT

    def _fill(**kw: Any) -> ClusterFillResult:
        batches.append(tuple(kw.get("tdd_frontier_keys") or ()))
        if grades is not None:
            grades.append(frozenset(kw.get("escalated_keys") or ()))
        if fill is not None:
            return fill(**kw)
        return ClusterFillResult(attempts=1)

    def _escalate(src_dir: Path, tname: str, units: set[FnLocator],
                  producers_out=None):
        escalations.append((tname, frozenset(units)))
        # Live `escalate_to_producers` writes the producer subset here.
        # Product-ahead ranking reads the cached additions instead, so a
        # stub that skips this still lifts; filling it matches the live
        # fire so persist's producer tuple is not empty on resume.
        if producers_out is not None:
            producers_out.add(_PROD)
        return {_PROD: {tname}}

    def _suspend(pd: Path, sd: Path, units, cl: str, tests) -> list[str]:
        if suspensions is not None:
            suspensions.append((frozenset(units), frozenset(tests)))
        return ["src/a.rs::fmt::impl Fmt"]

    def _attr(*a: Any, **k: Any) -> AttributionResult:
        if attribute is not None:
            return attribute()
        return AttributionResult(
            {_ACC: {"t2"}}, FailureKind.RUNTIME, {_ACC: {"t2"}})

    return PhaseCTddDeps(
        attribute_failures=_attr,
        build_test_reverse_map=lambda *a, **k: {},
        filter_test_targets=lambda frontier, src, **_k: frontier,
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
        scan_unimplemented_for_cluster=lambda *a, **k: StubInventory(),
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
        task2_fill_fns_one_by_one=_fill,
        escalate_to_producers=_escalate,
        suspend_futile_guidance=_suspend,
        load_tdd_floor=lambda *a, **k: floor.get("row"),
        save_tdd_floor=lambda p, c, row: floor.__setitem__("row", row),
        ledger_load=ledger_load,
        run_id=run_id,
        enumerate_fns=enumerate_fns,
        enumerate_types=enumerate_types,
    )


def _run(deps: PhaseCTddDeps, tmp_path: Path, monkeypatch, *,
         guard: str = "2", dispatch_cap: str = "0") -> None:
    monkeypatch.setenv("PHASE_C_TDD_BATCH_RATCHET", "0")
    # High enough that the plateau budget never terminates these two-sweep
    # processes: the terminator under test is the flat-score guard, exactly as
    # in the recorded run (whose stall stood at 1/3 when the guard stopped it).
    monkeypatch.setenv("TDD_MAX_STALLS", "9")
    # The guard is ON: the lock under test is the interaction between its
    # repeat rule and the escalation's two-sweep trigger.
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", guard)
    monkeypatch.setenv("TDD_REFILL_MAX_ATTEMPTS", "99")
    monkeypatch.setenv("PHASE_C_TDD_MAX_DISPATCH", dispatch_cap)
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
    assert "t2" in (row.get("escalation_products") or {}), (
        "after-fire persist must carry the product — GPU_GUARD_STOP fires "
        "before the dispatch save")


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
    assert batches and _PROD in batches[0], (
        f"iter 1's batch must hold the escalated producer, got {batches[0]}")
    assert (escalations or any("re-merged" in ln for ln in logs)), (
        "resume either re-fires from the trigger seed or re-merges the "
        "persisted product")
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
    row.pop("escalation_products", None)
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
    """When the escalation fires, the futile accessors' retro guidance is
    handed to the suspension hook — the record advises the exact rewrite
    the escalation just measured as changing nothing."""
    floor: dict = {}
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    suspensions: list[tuple[frozenset[FnLocator], frozenset[str]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations, suspensions=suspensions),
         tmp_path, monkeypatch)
    # Accessors first, then the producer subset the live fire writes to
    # `producers_out` (a stub that skipped that side channel used to hide
    # the second call).
    assert suspensions == [
        (frozenset({_ACC}), frozenset({"t2"})),
        (frozenset({_PROD}), frozenset({"t2"})),
    ]
    assert any("suspended stored guidance" in ln for ln in logs)


def test_futility_survives_a_code_move(tmp_path, monkeypatch) -> None:
    """The grade describes the TREE, so a `code_id` move — the shape of
    every validation launch — no longer voids the evidence: the resumed
    process escalates at its FIRST sweep and iter 1's batch carries the
    producer instead of re-buying the accessor traversal (the toll runs
    `21368-93f6a589` → `40938-f4e8e94b` paid twice in one evening on the
    same git tree, and the three runs of 2026-08-25 paid it once each —
    2h03m/2h18m/4h02m)."""
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
    assert "t2" in (floor["row"].get("escalation_products") or {}), (
        "the product must re-persist too — a resume that blanked it would "
        "re-buy accessors on the next attribution shift")


_NEW_ACC = FnLocator("src/a.rs", "other_accessor")


def test_a_kill_mid_fill_does_not_stamp_served(tmp_path, monkeypatch) -> None:
    """Dispatch persist advances the cursor; fill-return stamps served.
    A kill inside fill must leave the unit off the service record."""
    floor: dict = {}
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _die(**kw: Any) -> ClusterFillResult:
        raise KeyboardInterrupt("killed mid-fill")

    with pytest.raises(KeyboardInterrupt):
        _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
                   escalations=[], fill=_die), tmp_path, monkeypatch)
    row = floor.get("row") or {}
    assert "served" in row, "the field must be present (empty), not missing"
    assert not any("fmt" in k for k in (row.get("served") or [])), (
        "a rewrite that never returned is not service")


def test_unserved_futility_does_not_fire_on_resume(
        tmp_path, monkeypatch) -> None:
    """THE FIX. A floor that names the unit futile but not served — the
    `308884` → `2932` shape once served was a dispatch stamp — must NOT
    escalate at the resume's first sweep."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row["served"] = []
    floor["row"] = row

    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    assert any("NOT escalating" in ln for ln in logs)
    assert all(_PROD not in b for b in batches), (
        "an unserved fire must not inject producers")


def test_dispatch_time_served_reproduces_the_iter1_fire(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL: stamp served at dispatch (the pre-fix rule). The
    stopped process's fill DID return, so served already includes the
    accessor — resume injects the producer. That is 2932's iter-1 fire
    when the kill never happened and the stamp was honest, and it is
    what a dispatch-time stamp of a killed fill would have looked like."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    logs: list[str] = []
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(logs=logs, events=[], batches=batches, floor=floor,
               escalations=[]), tmp_path, monkeypatch)
    assert batches and _PROD in batches[0]


def test_a_persisted_product_survives_an_attribution_shift(
        tmp_path, monkeypatch) -> None:
    """The product is keyed by TEST. Under the live cap, iter 1 dispatches
    the producer, not the new accessor (runs `4911`/`2932`). Cap=0 cannot
    distinguish: both keys share one batch."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)

    logs: list[str] = []
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(
        logs=logs, events=[], batches=batches, floor=floor, escalations=[],
        attribute=lambda: AttributionResult(
            {_NEW_ACC: {"t2"}}, FailureKind.RUNTIME, {_NEW_ACC: {"t2"}}),
    ), tmp_path, monkeypatch, dispatch_cap="1")
    assert batches and batches[0] == (_PROD,), (
        f"under cap=1 iter 1 must dispatch the producer, not the new "
        f"accessor; got {batches[0]}")


def test_without_the_persisted_product_an_attribution_shift_re_buys_accessors(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL — the trigger seed keyed on exact accessor-set
    equality, so a shift spent iter 1 on the new accessors."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row.pop("escalation_products", None)
    floor["row"] = row

    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(
        logs=[], events=[], batches=batches, floor=floor, escalations=[],
        attribute=lambda: AttributionResult(
            {_NEW_ACC: {"t2"}}, FailureKind.RUNTIME, {_NEW_ACC: {"t2"}}),
    ), tmp_path, monkeypatch, dispatch_cap="1")
    assert all(_PROD not in b for b in batches), (
        "pre-product-persist shape: the shift re-buys the new accessors")
    assert batches and batches[0] == (_NEW_ACC,)


_OTHER = FnLocator("src/b.rs", "other")


def test_rebuy_is_computed_on_the_ranked_frontier_not_the_cut_batch(
        tmp_path, monkeypatch) -> None:
    """The detector reads the ranked frontier before take_batch. Run
    `9933-efe68f37` computed hits on the cut batch, so a unit a prior
    process already graded empty-delta that sat behind the cap was
    invisible until it was bought. Cap 1, two attributed units, prior
    row on the second: the hit is on service_order and re_buy even
    though the dispatched batch does not contain it."""
    prior = [LedgerEntry(
        key=LedgerKey(project=tmp_path.name, cluster="stable",
                      file=_OTHER.file, fn_name=_OTHER.fn_name),
        body_sha="x", bodies=("x",), sig_sha="s", layout_fp="f",
        run_id="349271-d876e4a9", verdict_delta=(),
        state=BodyState.PROVEN)]
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    _run(_deps(
        logs=[], events=events, batches=batches, floor={},
        escalations=[], ledger_load=lambda *a, **k: prior,
        run_id="this-process",
        attribute=lambda: AttributionResult(
            {_ACC: {"t2"}, _OTHER: {"t3"}}, FailureKind.RUNTIME,
            {_ACC: {"t2"}, _OTHER: {"t3"}}),
    ), tmp_path, monkeypatch, guard="9", dispatch_cap="1")
    buys = [d for ev, d in events if ev == "phase_c.re_buy"]
    assert buys and f"{_OTHER.file}::{_OTHER.fn_name}" in buys[0]["units"]
    assert f"{_ACC.file}::{_ACC.fn_name}" not in buys[0]["units"]
    orders = [d for ev, d in events if ev == "phase_c.dispatch.service_order"]
    assert orders and f"{_OTHER.file}::{_OTHER.fn_name}" in orders[0]["rebuy"]
    assert batches and _OTHER not in batches[0]


def test_rebuy_emits_at_dispatch_for_a_prior_process_empty_delta(
        tmp_path, monkeypatch) -> None:
    """Walk-shaped: `349271-d876e4a9`'s 7-of-7 accessor re-buy, here as one
    unit a prior process already graded with an empty delta."""
    prior = [LedgerEntry(
        key=LedgerKey(project=tmp_path.name, cluster="stable",
                      file=_ACC.file, fn_name=_ACC.fn_name),
        body_sha="x", bodies=("x",), sig_sha="s", layout_fp="f",
        run_id="349271-d876e4a9", verdict_delta=(),
        state=BodyState.PROVEN)]
    events: list[tuple[str, dict]] = []
    _run(_deps(logs=[], events=events, batches=[], floor={},
               escalations=[], ledger_load=lambda *a, **k: prior,
               run_id="this-process"),
         tmp_path, monkeypatch, guard="9")
    buys = [d for ev, d in events if ev == "phase_c.re_buy"]
    acc = f"{_ACC.file}::{_ACC.fn_name}"
    assert buys and acc in buys[0]["units"]
    # NEGATIVE CONTROL — a FLAT / not-FLAT cut put this PROVEN row in
    # unmeasured ("tests did not run"). The standing grade is proven.
    assert acc not in buys[0]["unmeasured"]
    assert acc not in buys[0]["flat"]
    assert buys[0]["grades"]["proven"] == [acc]


def test_rebuy_does_not_fire_on_the_same_process_or_after_a_code_move(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE: first dispatch (no prior), same run_id, and a code-move
    resume do not emit."""
    events: list[tuple[str, dict]] = []
    _run(_deps(logs=[], events=events, batches=[], floor={},
               escalations=[], run_id="this-process"),
         tmp_path, monkeypatch, guard="9")
    assert not any(ev == "phase_c.re_buy" for ev, _ in events)

    prior = [LedgerEntry(
        key=LedgerKey(project=tmp_path.name, cluster="stable",
                      file=_ACC.file, fn_name=_ACC.fn_name),
        body_sha="x", bodies=("x",), sig_sha="s", layout_fp="f",
        run_id="this-process", verdict_delta=(),
        state=BodyState.PROVEN)]
    events = []
    _run(_deps(logs=[], events=events, batches=[], floor={},
               escalations=[], ledger_load=lambda *a, **k: prior,
               run_id="this-process"),
         tmp_path, monkeypatch, guard="9")
    assert not any(ev == "phase_c.re_buy" for ev, _ in events)

    # Code-move: resume a floor from another identity; skip the detector.
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    row["code_id"] = "a-different-build"
    floor["row"] = row
    events = []
    _run(_deps(logs=[], events=events, batches=[], floor=floor,
               escalations=[], ledger_load=lambda *a, **k: prior,
               run_id="new-process"),
         tmp_path, monkeypatch)
    assert not any(ev == "phase_c.re_buy" for ev, _ in events)


# A writer that stored every frontier addition under the producer name.
# `widen` does not produce the earned accessor's type. `producer` does.
_ADMIT = FnLocator("src/c.rs", "widen")
_CELL_RECS = [
    {"file": "src/a.rs", "fn_name": "fmt", "container": "impl Cell",
     "sig_text": "fn fmt(&self) -> fmt::Result", "body_text": "Ok(())"},
    {"file": "src/b.rs", "fn_name": "producer", "container": "",
     "sig_text": "fn producer() -> Cell", "body_text": "Cell { n: 1 }"},
    {"file": "src/c.rs", "fn_name": "widen", "container": "",
     "sig_text": "fn widen(p: usize) -> usize", "body_text": "p"},
]
_CELL_TYPES = [TypeRecord(kind="struct", name="Cell")]


def _contaminate(floor: dict) -> None:
    """Put a non-producer into the saved producer tuple and the cached
    additions, the shape a floor written before the split resumes with."""
    body = floor["row"]["escalation_products"]["t2"]
    prods = list(body.get("producers") or [])
    if f"{_ADMIT.file}::{_ADMIT.fn_name}" not in prods:
        prods.append(f"{_ADMIT.file}::{_ADMIT.fn_name}")
    body["producers"] = prods
    adds = dict(body.get("additions") or {})
    adds[f"{_ADMIT.file}::{_ADMIT.fn_name}"] = ["t2"]
    body["additions"] = adds


def _resume_contaminated(tmp_path, monkeypatch, **dep_kw):
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    _contaminate(floor)
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    grades: list[frozenset[FnLocator]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=[], grades=grades, **dep_kw),
         tmp_path, monkeypatch)
    return floor, logs, events, batches, grades


def test_a_resumed_producer_grade_drops_a_saved_name_the_scan_does_not(
        tmp_path, monkeypatch) -> None:
    """The fill's escalated set is the scan of the earned accessors. The
    fn that hands their type back stays. The saved name that does not
    produce it is still on the frontier and is not graded as futility."""
    floor, logs, events, batches, grades = _resume_contaminated(
        tmp_path, monkeypatch,
        enumerate_fns=lambda _sd: _CELL_RECS,
        enumerate_types=lambda _sd: _CELL_TYPES)
    assert grades and _PROD in grades[0] and _ADMIT not in grades[0]
    assert batches and _ADMIT in batches[0] and _PROD in batches[0]
    rederived = [d for ev, d in events
                 if ev == "phase_c.attribution.producers_rederived"]
    assert rederived and rederived[0]["test"] == "t2"
    assert "src/b.rs::producer" in rederived[0]["producers"]
    assert "src/c.rs::widen" in rederived[0]["dropped"]
    assert "src/c.rs::widen" not in rederived[0]["producers"]
    assert any("re-derived from the tree" in ln for ln in logs)
    saved = floor["row"]["escalation_products"]["t2"]["producers"]
    assert saved == ["src/b.rs::producer"]
    assert "src/c.rs::widen" in (
        floor["row"]["escalation_products"]["t2"]["additions"])


def test_without_the_tree_records_the_saved_admit_stays_escalated(
        tmp_path, monkeypatch) -> None:
    """NEGATIVE CONTROL. No fn or type enumerator is wired, so an empty
    scan is not a measurement and the saved tuple still grades the admit."""
    _floor, logs, events, batches, grades = _resume_contaminated(
        tmp_path, monkeypatch)
    assert grades and _PROD in grades[0] and _ADMIT in grades[0]
    assert batches and _ADMIT in batches[0]
    assert not any(ev == "phase_c.attribution.producers_rederived"
                   for ev, _ in events)
    assert any("grade left as saved" in ln for ln in logs)


def test_a_completed_scan_that_names_nobody_replaces_the_saved_tuple(
        tmp_path, monkeypatch) -> None:
    """The records were read and no fn produces the accessor's type. That
    empty result replaces the saved tuple, admit and all."""
    other = [
        {**r, "sig_text": "fn producer() -> usize", "body_text": "1"}
        if r["fn_name"] == "producer" else r
        for r in _CELL_RECS
    ]
    floor, logs, events, batches, grades = _resume_contaminated(
        tmp_path, monkeypatch,
        enumerate_fns=lambda _sd: other,
        enumerate_types=lambda _sd: _CELL_TYPES)
    assert grades and _PROD not in grades[0] and _ADMIT not in grades[0]
    assert batches and _ADMIT in batches[0] and _PROD in batches[0]
    rederived = [d for ev, d in events
                 if ev == "phase_c.attribution.producers_rederived"]
    assert rederived and rederived[0]["producers"] == []
    assert "src/b.rs::producer" in rederived[0]["dropped"]
    assert "src/c.rs::widen" in rederived[0]["dropped"]
    assert any("0 producer(s)" in ln for ln in logs)
    assert floor["row"]["escalation_products"]["t2"]["producers"] == []


def test_a_pre_split_floor_re_earns_the_verified_view_and_keeps_the_fire(
        tmp_path, monkeypatch) -> None:
    """The verified-service split. A floor from before `served_verified`
    carries stamps the batch-wide rule may have written (run `9933-efe68f37`
    iter 1: 12 stamped, `wanted` was `{context, msg}`) and nothing can now
    distinguish them. On resume the ranking's view starts EMPTY and re-earns
    stamps as routes open units; the fire's inclusive view keeps every stamp,
    so the resumed escalation still fires at its first sweep."""
    floor: dict = {}
    _stopped_process(tmp_path, monkeypatch, floor)
    row = dict(floor["row"])
    # A pre-split floor: inclusive stamps only, one of them unverifiable
    # (the producer was never route-opened by any process).
    row.pop("served_verified", None)
    row["served"] = sorted(set(row.get("served") or [])
                           | {"src/b.rs::producer"})
    floor["row"] = row

    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []
    escalations: list[tuple[str, frozenset[FnLocator]]] = []
    _run(_deps(logs=logs, events=events, batches=batches, floor=floor,
               escalations=escalations), tmp_path, monkeypatch)
    # The inclusive view still arms the resumed fire: the producer reaches
    # a batch exactly as in the unmodified resume.
    assert any(_PROD in b for b in batches)
    saved = floor.get("row") or {}
    verified = set(saved.get("served_verified") or [])
    # Re-earned from this process's own route-scoped returns only — the
    # inherited unverifiable stamp is NOT adopted…
    assert verified, "the resumed process re-earns verified stamps"
    assert verified <= {f"{k.file}::{k.fn_name}"
                       for b in batches for k in b}, (
        "verified stamps only for units a route actually returned")
    # …while the inclusive record keeps it for the fire's premise.
    assert "src/b.rs::producer" in set(saved.get("served") or [])
