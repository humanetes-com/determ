"""Execute one fill as a schedule of route runs.

`execute_fill` asks every route what it would do (`FillRoute.plan`), orders
those runs with `domain.fill_schedule.schedule` against the route ledger
and this fill's hollow relation, allots the fill budget, and runs them one
at a time under a `RunClock`. Around each run it takes the suite reading
that every route's payoff is judged by — the same `measure_test_state` the
regression gate uses — so a session's turn, a file-first landing and a
per-fn pass are all scored by one instrument and written to one ledger.

Under `scheduled=False` the executor reproduces the fill's pre-schedule
walk exactly: the routes' own plan order, today's route precedence, no
ledger, no budget. That is the negative control for the ordering rules and
the A/B lever (`PHASE_C_FILL_SCHEDULE=0`).

What this module does not do: judge a landed change set (the gate does),
interrupt a run (the clock only gates the next claim), or stop the process
(`check_stop` is the graceful-stop / fill-phase-clock seam and raises).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from pipeline_core.application.reconstruct.fill_reading import (
    kept_mark,
    kept_since,
)
from pipeline_core.application.reconstruct.fill_routes import (
    FillRoute,
    RouteLedger,
    RouteRunReport,
)
from pipeline_core.application.reconstruct.unit_pool import run_units_concurrently
from pipeline_core.domain import unit_clock
from pipeline_core.domain.body_ledger import ReportKeep, WaveKept
from pipeline_core.domain.fill_asks import FillAsks
from pipeline_core.domain.fill_context import route_scope
from pipeline_core.domain.fill_schedule import (
    CrashBlame,
    FillBudget,
    FillSchedule,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
    ScheduledRun,
    UnitEvidence,
    call_price,
    premise_refused_last_fill,
    release_held,
    schedule,
    withhold_landed,
)
from pipeline_core.domain.held_stubs import Blockers, HeldStubs
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.reasoning_cap import observe_call, run_all_replies_capped
from pipeline_core.domain.schedule_invariants import (
    decided_from_schedule,
    violations,
)
from pipeline_core.domain.test_outcome import (
    PremiseEntry,
    ground_left,
    moved_in_scope,
    names_of,
    premise_moved,
    standing_in,
)

__all__ = ["FillExecution", "FillExecutionDeps", "execute_fill"]


@dataclass(frozen=True, slots=True)
class FillExecutionDeps:
    log: Callable[[str], None]
    emit: Callable[..., None]
    check_stop: Callable[[str], None]
    ledger: RouteLedger | None
    project: str
    cluster: str
    run_id: str
    iteration: int
    oracle_fp: str
    budget_minutes: float = 0.0
    scheduled: bool = True
    readmit_after: int = 2
    #: Worker count for a wave of a parallel route's consecutive runs.
    parallelism: int = 1
    now: Callable[[], float] = time.monotonic
    #: Bare names of leftover tests whose binary did not complete. A run
    #: whose every aimed test is in this set, and whose targets miss
    #: ``crash_blamed``, ranks as a flat re-buy. Empty (the default)
    #: leaves every rank as the ledger would.
    unmeasurable_tests: frozenset[str] = frozenset()
    #: Crash culprit -> the tests whose crash convicted it
    #: (`CrashSites.blame_map`); the schedule's blocker predicate asks a
    #: session for a culprit only when it aims at one of those tests.
    crash_blamed: CrashBlame = field(default_factory=dict)
    #: How many runs of a route kind one fill opens (`schedule`'s
    #: ``max_per_kind``); the rest are deferred to the next fill. Empty =
    #: unbounded. The session cap is `SLICE_SESSION_MAX_PER_FILL`.
    max_per_kind: Mapping[RouteKind, int] = field(default_factory=dict)
    #: A session's share floor in turns (`schedule`'s ``session_min_turns``,
    #: `SLICE_SESSION_MIN_TURNS`); 0 = the plain weighted split.
    session_min_turns: int = 0
    #: Each claimed unit's wall-clock bound as a multiple of its run's
    #: share (`FILL_UNIT_SHARE_MULTIPLE`, `unit_clock`); 0 = unbounded.
    unit_share_multiple: float = 0.0
    #: Locators leftover tests convict before measured-owners admission
    #: (eligible plus hop-pair members). Empty (the default) skips
    #: coverage-split marking, so every recorded fill fixture's order is
    #: unchanged by this argument. Snapshot before owner-map admission
    #: or the spine locators become "attributed".
    attributed: frozenset[FnLocator] = frozenset()
    #: The four facts the schedule check turns into the owed owners
    #: (`measured_owners_owed`, inside `violations`). Admitted owners, the
    #: ordered admits, the dispatch batch, and the owners the rank left out
    #: at its rotation bound. The check does not take a precomputed set: a
    #: caller could pass the whole frontier and name an owner the rank left
    #: inside its bound. Empty (the default) checks nothing.
    owner_admitted: frozenset[FnLocator] = frozenset()
    owner_ordered: tuple[FnLocator, ...] = ()
    owner_batch: frozenset[FnLocator] = frozenset()
    owner_starved: frozenset[FnLocator] = frozenset()
    #: Defer a non-session run every target of which an earlier run of this
    #: fill already asked under evidence nothing has moved since
    #: (`fill_asks.FillAsks`). False keeps the shadow line only.
    defer_reasked: bool = True
    #: What the red tests owe this fill (`candidate_tiers.due_reading`): a
    #: tier of an executed slice no prompt has asked under the current
    #: evidence. Each gets an opened writer whatever deferred it
    #: (`fill_schedule._cover_due`); one with none is `slice_unit_starved`.
    #: Empty (the default) changes nothing.
    due: frozenset[FnLocator] = frozenset()
    #: Each owed function's ladder rung (`candidate_tiers.LADDER`): a
    #: function owed on the session rung is covered only by an opened
    #: session run. Empty: every owed function is owed on the unit rung.
    due_rungs: Mapping[FnLocator, str] = field(default_factory=dict)
    #: The body ledger's per-unit verdicts (`fill_schedule.UnitEvidence`):
    #: every unit-writing run is ranked on its units' own graded landings.
    #: None keeps the route-row reading (no body ledger on this driver).
    unit_evidence: UnitEvidence | None = None
    #: How many body-ledger rows `unit_evidence` was built from, in cluster
    #: file order. Recorded on the schedule event so a replay cuts the
    #: ledger where this fill ranked. None on a caller that did not count.
    body_rows: int | None = None
    #: Called once per wave, after the wave's route rows are flushed, with
    #: the units the wave's reports say they kept — and once more on the
    #: wave a graceful stop cut, before the stop propagates. The driver's
    #: unit-ledger writer sits behind it: the per-unit `landed` rows used
    #: to be written only after the whole fill returned, so a fill that
    #: ended on a stop left every kept body without a row (four fills of
    #: 2026-09-08, about sixty bodies, zero rows). The route ledger is
    #: flushed per wave for the same reason; this is the unit ledger's
    #: turn. None (the default) keeps the post-fill write alone.
    on_wave_kept: Callable[[frozenset[FnLocator]], object] | None = None
    #: The ground the plan was made from (`test_outcome.premise_of` of the
    #: sweep's capture) and a reader of the ground the tree stands on now
    #: (one suite run → the same function). With both set, a claim planned
    #: on a test a keep moved is refused (`_GroundGate`); runs whose planned
    #: tests did not move keep going. None (the default) runs the plan to
    #: its end —
    #: the frozen-plan negative control (`PHASE_C_REPLAN_ON_PREMISE=0`).
    premise: Mapping[str, PremiseEntry] | None = None
    ground_now: Callable[[], Mapping[str, PremiseEntry] | None] | None = None


@dataclass(frozen=True, slots=True)
class FillExecution:
    schedule: FillSchedule
    rows: tuple[RouteLedgerRow, ...]
    stopped: bool = False
    #: The tests whose ground moved in this fill (empty when no keep moved
    #: one). Runs and units planned on them were refused.
    premise_moved: tuple[str, ...] = ()


class _GroundGate:
    """The fill's premise gate, shared by every run's clock.

    Answers per claim: has ALL the ground THIS claim was planned on moved
    (`test_outcome.ground_left` empty)? A claim's scope is the tests that put
    it on the plan — a pooled unit's own attributed tests, else its run's
    target tests; a claim with no named test stands on the whole ground. A
    claim with some planned tests unmoved stands on them: the second form
    refused on any one moved test, which is the fill-wide rule again for a
    wide run (run `116341-24c76a97`, `test_outcome.ground_left`).
    The first form refused every claim of the fill on any move, so on a
    hollow tree each fill ended at its first keep: run `497151-a6fa205b`
    (2026-09-22) kept `src/error.rs::new`, its 13 held tests moved, and the
    8 unclaimed units of a 12-unit batch were refused with none of their 55
    held tests moved — three iterations in a row landed only the in-flight
    wave. A keep that moves every failing test (run `3383938-c5d24499`,
    141 of 141) hits every claim's scope; what continues is only the claims
    the new ground still stands (below) — the units the moved tests now stop
    in — and the rest waits for the sweep's re-plan.

    Measures only when something was kept since the last look
    (`fill_reading.kept_mark`), so a fill that keeps nothing never pays for
    it. The moved set only grows within a fill, against the plan's premise.
    Thread-safe: a parallel wave's workers ask at their own unit
    boundaries.

    Each growth of the moved set is logged here, where it is first seen
    (run `311306-c8624d12`, 2026-09-22: a pooled per-fn run stopped with
    its unclaimed units unnamed).

    A claim whose planned ground moved still stands when the ground as it is
    now puts a failing test's first failure inside a unit the claim writes
    (`test_outcome.standing_in`), whether or not the claim was planned on
    that test: the move is evidence for it, and a re-plan would dispatch it
    on exactly that reading. The units are a
    required argument, so no claim can be asked without them. Scoped refusal
    alone refused such holders fill after fill on a stub chain: run
    `790176-f0df7402` (2026-09-23) refused `downcast_ref` and `is` in fills
    1 and 2 while every sweep read them holding tests."""

    def __init__(self, deps: FillExecutionDeps,
                 spared: frozenset[FnLocator] = frozenset()) -> None:
        self._premise = deps.premise
        #: Units the previous fill already refused on premise
        #: (`fill_schedule.premise_refused_last_fill`): not refused again.
        self._spared = spared
        self._read = deps.ground_now
        self._log = deps.log
        self._iteration = deps.iteration
        self._lock = threading.Lock()
        self._seen = kept_mark()
        self._now: Mapping[str, PremiseEntry] = {}
        self._admitted: set[frozenset[tuple[str, str]]] = set()
        self._partial: set[frozenset[str]] = set()
        self.moved: tuple[str, ...] = ()
        self.refused = 0
        self.admitted = 0

    @property
    def armed(self) -> bool:
        return self._premise is not None and self._read is not None

    def _look(self) -> None:
        """Re-read the ground if something was kept since the last look.
        Caller holds the lock."""
        mark = kept_mark()
        if mark == self._seen:
            return
        self._seen = mark
        assert self._read is not None and self._premise is not None
        now = self._read()
        if now is None:
            return
        self._now = now
        grown = set(self.moved) | set(premise_moved(self._premise, now))
        if len(grown) == len(self.moved):
            return
        new = sorted(grown - set(self.moved))
        self.moved = tuple(sorted(grown))
        self._log(
            f"  [phase-c/schedule] fill {self._iteration}: premise moved "
            f"after a keep — {len(new)} test(s) no longer fail where the "
            f"plan read them ({names_of(new)}); {len(self.moved)} moved in "
            "this fill. Claims planned on a moved test are refused, the "
            "rest of the plan continues, units in flight complete")

    def __call__(self, scope: frozenset[str] | None,
                 units: frozenset[FnLocator]) -> bool:
        """True when the claim is refused: its WHOLE planned ground moved
        (`test_outcome.ground_left` is empty) and the ground now holds none
        of its planned tests inside ``units``. A claim some of whose planned
        tests still read as the plan read them stands on those."""
        if not self.armed:
            return False
        with self._lock:
            self._look()
            if not self.moved:
                return False
            if not moved_in_scope(self.moved, scope):
                return False
            assert self._premise is not None
            left = ground_left(self.moved, scope, self._premise.keys())
            if left:
                key = frozenset(scope or ())
                if key not in self._partial:
                    self._partial.add(key)
                    self._log(
                        f"  [phase-c/schedule] fill {self._iteration}: "
                        f"claim on partly moved ground stands — "
                        f"{len(left)} of {len(key)} planned test(s) still "
                        f"read as planned ({names_of(left)})")
                return False
            pairs = frozenset((u.file, u.fn_name) for u in units)
            standing = standing_in(self._now, pairs)
            if standing:
                self.admitted += 1
                if pairs not in self._admitted:
                    self._admitted.add(pairs)
                    self._log(
                        f"  [phase-c/schedule] fill {self._iteration}: "
                        f"claim on moved ground admitted — "
                        f"{len(standing)} failing test(s) now first-fail "
                        f"inside {names_of(sorted(f'{f}::{n}' for f, n in pairs))} "
                        f"({names_of(standing)})")
                return False
            spared = frozenset(units) & self._spared
            if spared:
                self.admitted += 1
                if pairs not in self._admitted:
                    self._admitted.add(pairs)
                    self._log(
                        f"  [phase-c/schedule] fill {self._iteration}: "
                        f"claim on moved ground stands — the previous fill "
                        f"already refused "
                        f"{names_of(sorted(f'{u.file}::{u.fn_name}' for u in spared))} "
                        "on premise; it is not refused twice running")
                return False
            self.refused += 1
            return True


def _notify_wave(deps: FillExecutionDeps,
                 reports: Sequence[RouteRunReport | None],
                 rows: Sequence[RouteLedgerRow] = ()) -> None:
    """Hand a finished (or stopped) wave's kept units to the driver's
    unit-ledger writer, each report's keep beside them (`WaveKept.reports`:
    the units that report kept and its row's `newly_green`, rows in report
    order). The writer decides which unit a green test is credited to
    (`body_ledger.credited_green`), with the attribution it holds. A wave
    whose every report is `empty` opened nothing and is not reported; a
    failure in the writer is logged and never ends the fill — the ledger
    records the fill, it does not run it."""
    if deps.on_wave_kept is None:
        return
    present = [r for r in reports if r is not None]
    if present and all(r.empty for r in present):
        return
    keeps: list[ReportKeep] = []
    for n, rep in enumerate(reports):
        if rep is None or not rep.kept:
            continue
        row = rows[n] if n < len(rows) else None
        keeps.append(ReportKeep(
            frozenset(rep.kept),
            None if row is None or row.newly_green is None
            else frozenset(row.newly_green)))
    kept = WaveKept(frozenset(u for r in present for u in r.kept), keeps)
    try:
        deps.on_wave_kept(kept)
    except Exception as exc:  # noqa: BLE001 — the ledger never ends a fill
        deps.log(f"  [phase-c/schedule] wave ledger write failed "
                 f"({type(exc).__name__}: {exc})")


def _outcome(report: RouteRunReport, delta: int) -> RunOutcome:
    if report.stopped:
        return RunOutcome.STOPPED
    if report.empty:
        return RunOutcome.EMPTY
    if report.declined:
        return RunOutcome.DECLINED
    if delta > 0:
        return RunOutcome.CASHED
    if report.unmeasured:
        # Nothing this run landed came from a completed deliberation; a
        # verdict that did not move under a truncated call is not FLAT.
        return RunOutcome.UNMEASURED
    if report.reverted and not report.kept:
        return RunOutcome.REVERTED
    return RunOutcome.FLAT


def _landed_nothing(
    rep: RouteRunReport, targets: frozenset[FnLocator], before: str,
    bodies_sha: Callable[[frozenset[FnLocator]], str],
) -> RouteRunReport:
    """`rep` with its keeps dropped when they changed no body.

    A route reports what its gate KEPT, and a reply that re-emits the bodies
    already on disk passes every gate: it compiles and regresses nothing.
    Run `3893442-cb547388` (2026-09-19) rowed both hop pairs `kept 2` — the
    log said `FILLED 2 fn(s)` — while the checkpoint after the fill differed
    from the one before only in a file neither pair writes, and the
    landed-change audit printed both pairs' units `unchanged`. A keep that
    changed nothing is not a landing: the unit ledger, the route row and the
    log must not read it as one. The run stays a graded try (its reply is
    the evidence that this prompt yields the tree), so the row keeps its
    FLAT / UNMEASURED grade and the re-buy detector still sees it.

    Judged over the run's targets only: a keep outside them (an installable
    complement) cannot be compared against `before`, so it is left alone."""
    if not rep.kept or not before or not rep.kept <= targets:
        return rep
    if bodies_sha(targets) != before:
        return rep
    return replace(rep, kept=frozenset(), unchanged=True)


def _pairs(locs: frozenset[FnLocator]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((loc.file, loc.fn_name) for loc in locs))


_Kept = tuple[int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]


def _row_for(
    s: ScheduledRun, report: RouteRunReport | None, kept: _Kept | None,
    *, deps: FillExecutionDeps, started: str, minutes: float,
    share: float | None, outcome: RunOutcome | None = None,
) -> RouteLedgerRow:
    delta = 0
    red: tuple[str, ...] = ()
    crashed: tuple[str, ...] = ()
    # Which tests the run turned green. None when no keep reading was taken
    # for this row (deferred / budget-spent / re-graded at open): that row
    # opened no judged try, and "unrecorded" must not read as "paid nobody".
    green: tuple[str, ...] | None = None
    if kept is not None:
        green = kept[3]
    if report is not None and report.own_delta is not None:
        delta = report.own_delta
    elif kept is not None:
        delta, red, crashed, _green = kept
    rep = report or RouteRunReport()
    return RouteLedgerRow(
        kind=s.run.kind, label=s.run.label, targets=_pairs(s.run.targets),
        bodies_sha=s.run.bodies_sha, oracle_fp=deps.oracle_fp,
        outcome=outcome if outcome is not None else _outcome(rep, delta),
        run_id=deps.run_id, iteration=deps.iteration, cluster=deps.cluster,
        started=started, minutes=minutes, model_calls=rep.model_calls,
        net_pass_delta=delta,
        newly_red=red, newly_crashed=crashed, newly_green=green,
        filtered=tuple(sorted((u.file, u.fn_name, why)
                              for u, why in rep.filtered.items())),
        kept=_pairs(rep.kept), reverted=_pairs(rep.reverted),
        collateral=_pairs(rep.collateral & rep.reverted),
        target_tests=tuple(sorted(s.run.target_tests)),
        classes=dict(rep.classes), share_minutes=share, reason=s.reason,
        joint_sets=tuple(_pairs(frozenset(js)) for js in rep.joint_sets),
        opened=(_pairs(rep.opened_units) if report is not None else None),
        first_turn_minutes=rep.first_turn_minutes,
        carried_body=rep.carried_body,
        candidate_bodies=dict(rep.candidate_bodies),
        joint_repaired=bool(getattr(rep, "joint_repaired", False)))


def _log_row(deps: FillExecutionDeps, row: RouteLedgerRow, *,
             unchanged: bool = False) -> None:
    deps.log(
        f"  [phase-c/route] {row.label}: {row.outcome.value.upper()} after "
        f"{row.minutes:.1f} min ({row.model_calls} call(s), net "
        f"{row.net_pass_delta:+d}, kept {len(row.kept)}, reverted "
        f"{len(row.reverted)}"
        + (", every kept body was the one already on disk"
           if unchanged else "")
        + (f", newly red {len(row.newly_red)}" if row.newly_red else "")
        + (f", crashed {list(row.newly_crashed)}" if row.newly_crashed else "")
        + f"; {row.reason})")
    deps.emit("phase_c.route.run",
              data={**row.to_dict(), **({"unchanged": True} if unchanged else {})})


def _check_schedule(sched: FillSchedule,
                    hollow: Mapping[str, frozenset[FnLocator]],
                    deps: FillExecutionDeps) -> None:
    """Hold the decision to the invariants every schedule must satisfy
    (`domain/schedule_invariants.py`) and say so loudly when it does not.

    The schedule was corrected one incident at a time for a month and each
    correction left a sibling of its hole open; the one found on run
    `82925-203ed7b8` (2026-09-28) cost two fills before anyone read the
    schedule lines. This does not change the decision — the schedule is
    built to satisfy the invariants — it makes the next hole a log line on
    the fill that opens it."""
    if not deps.scheduled:
        return
    found = violations(
        decided_from_schedule(sched), hollow=hollow,
        crash_blamed=deps.crash_blamed,
        budget_minutes=sched.budget_minutes,
        admitted=deps.owner_admitted,
        ordered=deps.owner_ordered,
        batch=deps.owner_batch,
        starved=deps.owner_starved,
        due=deps.due)
    if not found:
        return
    deps.log(f"  HEALTH ‼ SCHEDULE_INVARIANT fill {deps.iteration}: "
             + "; ".join(v.render() for v in found))
    deps.emit("phase_c.fill.schedule_invariant", data={
        "cluster": deps.cluster, "iter": deps.iteration,
        "violations": [{"kind": v.kind, "detail": v.detail} for v in found]})


def _schedule_inputs(deps: FillExecutionDeps,
                     hollow: Mapping[str, frozenset[FnLocator]],
                     ) -> dict[str, object]:
    """The arguments `schedule` was called with, in the event's shape.

    An unscheduled fill passes the empty collections the call passes, so a
    replay of that fill does not apply inputs the ranking never read.
    `standing_flat` is omitted when this fill did not rank on the body
    ledger: a present map, even an empty one, is the set the ranking used.
    """
    scheduled = deps.scheduled
    inputs: dict[str, object] = {
        "hollow": (
            {t: sorted(f"{u.file}::{u.fn_name}" for u in locs)
             for t, locs in sorted(hollow.items())}
            if scheduled else {}),
        "crash_blamed": (
            sorted(f"{u.file}::{u.fn_name}" for u in deps.crash_blamed)
            if scheduled else []),
        "crash_blamed_tests": (
            {f"{u.file}::{u.fn_name}": sorted(ts)
             for u, ts in sorted(Blockers.of({}, deps.crash_blamed)
                                 .blamed.items()) if ts}
            if scheduled else {}),
        "attributed": (
            sorted(f"{u.file}::{u.fn_name}" for u in deps.attributed)
            if scheduled else []),
        "unmeasurable_tests": (
            sorted(deps.unmeasurable_tests) if scheduled else []),
        "readmit_after": deps.readmit_after,
        "session_min_turns": deps.session_min_turns if scheduled else 0,
        "max_per_kind": (
            {k.value: v for k, v in (deps.max_per_kind or {}).items()}
            if scheduled else {}),
    }
    if scheduled and any((deps.owner_admitted, deps.owner_ordered,
                          deps.owner_batch, deps.owner_starved)):
        inputs["owner_admitted"] = [
            f"{u.file}::{u.fn_name}" for u in deps.owner_admitted]
        inputs["owner_ordered"] = [
            f"{u.file}::{u.fn_name}" for u in deps.owner_ordered]
        inputs["owner_batch"] = [
            f"{u.file}::{u.fn_name}" for u in deps.owner_batch]
        inputs["owner_starved"] = [
            f"{u.file}::{u.fn_name}" for u in deps.owner_starved]
    if scheduled and deps.due:
        inputs["due"] = sorted(f"{u.file}::{u.fn_name}" for u in deps.due)
    if scheduled and deps.unit_evidence is not None:
        inputs["standing_flat"] = {
            f"{u.file}::{u.fn_name}": sha
            for u, sha in sorted(deps.unit_evidence.standing_flat.items(),
                                 key=lambda p: (p[0].file, p[0].fn_name))}
        if deps.body_rows is not None:
            inputs["body_rows"] = deps.body_rows
    return inputs


def _withheld_clauses(withheld: Sequence[str], open_names: Sequence[str], *,
                      deferred: bool) -> str:
    """The tail of the withheld-targets line: the held names after
    `withheld:` (or `deferred:` when nothing opens), and EVERY name the run
    opens on after `opens on the other N:`. The open clause is not capped:
    a wake reads it as what the run is spending its share on, and on run
    `473471-33301154` (2026-09-29, fill 6) the open set was six names with
    the cfg-dead one sorting last, so a four-name head would have hidden
    it. The previous line printed the held names after `opens on the other
    N`, telling the reader the run spent its share on units it withheld."""
    if deferred:
        return f"deferred: {names_of(withheld)}"
    return (f"withheld: {names_of(withheld)}; "
            f"opens on the other {len(open_names)}: {', '.join(open_names)}")


def _withhold_landed_wave(
    wave: Sequence[ScheduledRun], landed: set[FnLocator], *,
    deps: FillExecutionDeps, started: str,
) -> tuple[list[ScheduledRun], list[RouteLedgerRow], set[str]]:
    """The wave with every run's already-landed targets withheld
    (`fill_schedule.withhold_landed`), and a DEFERRED row per run that had
    nothing left to open, and the keys of those runs. A run keeps its
    planned share: the targets it lost were not its to spend on."""
    frozen = frozenset(landed)
    opened: list[ScheduledRun] = []
    deferred: list[RouteLedgerRow] = []
    gone: set[str] = set()
    for w in wave:
        run_now, held = withhold_landed(w.run, frozen)
        if not held:
            opened.append(w)
            continue
        names = sorted(f"{u.file}::{u.fn_name}" for u in held)
        open_names = (sorted(f"{u.file}::{u.fn_name}" for u in run_now.targets)
                      if run_now is not None else [])
        deps.log(
            f"  [phase-c/schedule] fill {deps.iteration}: {w.run.label} — "
            f"{len(held)} target(s) an earlier run of this fill kept a body "
            "for are withheld until the sweep grades them; "
            + _withheld_clauses(names, open_names, deferred=run_now is None))
        deps.emit("phase_c.fill.landed_withheld", data={
            "cluster": deps.cluster, "iter": deps.iteration,
            "run": w.run.label, "withheld": names,
            "deferred": run_now is None, "open": open_names})
        if run_now is None:
            row = replace(
                _row_for(w, None, None, deps=deps, started=started,
                         minutes=0.0, share=0.0, outcome=RunOutcome.DEFERRED),
                reason="landed_this_fill")
            deferred.append(row)
            gone.add(w.run.key)
            _log_row(deps, row)
            continue
        opened.append(replace(w, run=run_now))
    return opened, deferred, gone


def execute_fill(
    routes: Sequence[FillRoute],
    *,
    hollow: Mapping[str, frozenset[FnLocator]],
    bodies_sha: Callable[[frozenset[FnLocator]], str],
    deps: FillExecutionDeps,
) -> FillExecution:
    """Plan, schedule and run every route of one fill. Returns the schedule
    and the rows written; `stopped` when a graceful stop ended it (the stop
    itself propagates through `check_stop`, so this is what a caller that
    catches `SystemExit` upstream reads)."""
    by_key: dict[str, FillRoute] = {}
    runs: list[RouteRun] = []
    for route in routes:
        for seq, run in enumerate(route.plan()):
            if not run.bodies_sha:
                # `seq` is the run's index in plan() and part of `key`.
                # Every route stamps it from the same enumerate this loop
                # runs, so a stamped value and the loop index agree, 0
                # included; a route that left the default gets the loop
                # index. `run.seq or seq` therefore never changes a
                # stamped 0 into anything but 0.
                run = replace(
                    run, seq=run.seq or seq,
                    bodies_sha=bodies_sha(run.targets))
            by_key[run.key] = route
            runs.append(run)
    prior: list[RouteLedgerRow] = []
    if deps.scheduled and deps.ledger is not None:
        prior = deps.ledger.load(deps.project, deps.cluster)
    sched = schedule(
        runs, prior, oracle_fp=deps.oracle_fp,
        hollow=hollow if deps.scheduled else {},
        budget_minutes=deps.budget_minutes if deps.scheduled else 0.0,
        readmit_after=deps.readmit_after,
        unmeasurable_tests=deps.unmeasurable_tests if deps.scheduled else (),
        crash_blamed=deps.crash_blamed if deps.scheduled else (),
        max_per_kind=deps.max_per_kind if deps.scheduled else None,
        attributed=deps.attributed if deps.scheduled else (),
        session_min_turns=deps.session_min_turns if deps.scheduled else 0,
        bodies_sha=bodies_sha,
        unit_evidence=deps.unit_evidence if deps.scheduled else None,
        due=deps.due if deps.scheduled else (),
        due_rungs=deps.due_rungs if deps.scheduled else None)
    deps.log(
        f"  [phase-c/schedule] fill {deps.iteration}: {len(sched.ordered)} "
        f"run(s) — {sched.render()}"
        + ("" if deps.scheduled else " (fixed order: PHASE_C_FILL_SCHEDULE=0)")
        + (f"; budget {sched.budget_minutes:.0f} min" if sched.budget_minutes else
           "; budget unbounded")
        + f"; hollow {len(hollow)} test(s); ledger {len(prior)} row(s)")
    deps.emit("phase_c.fill.schedule", data={
        "cluster": deps.cluster, "iter": deps.iteration,
        "scheduled": deps.scheduled, "budget_minutes": sched.budget_minutes,
        "hollow": len(hollow), "ledger_rows": len(prior),
        "runs": [s.to_dict() for s in sched.ordered],
        "deferred": [s.to_dict() for s in sched.deferred],
        # The schedule's other inputs, so a recorded fill can be replayed
        # as it was decided (`scripts/exemption_replay.py`). Without them
        # the 2026-09-23 exemption review could not replay a coverage-split
        # deferral at all (it needs `attributed`) and replayed every run's
        # blocker tier flat. The values are the ones `schedule` was called
        # with: an unscheduled fill passes empty hollow, blame, attribution
        # and caps, and `readmit_after` is the one argument it still passes.
        # `standing_flat` is present only when this fill ranked on the body
        # ledger. Its values are body shas — a name with no sha cannot found
        # a flat landing, because the grade matches the sha. `body_rows` is
        # the ledger length that verdict was built from.
        "inputs": _schedule_inputs(deps, hollow)})
    _check_schedule(sched, hollow, deps)

    budget = FillBudget(sched.budget_minutes, now=deps.now)
    rows: list[RouteLedgerRow] = []
    stopped = False
    ordered = list(sched.ordered)
    _rows_for_gate = prior if prior or deps.ledger is None else (
        deps.ledger.load(deps.project, deps.cluster))
    gate = _GroundGate(deps, premise_refused_last_fill(
        _rows_for_gate, cluster=deps.cluster, run_id=deps.run_id,
        iteration=deps.iteration))
    # The held stubs of this fill: a run's share never skips one it was
    # given to write (`RunClock.owed`).
    held_units = (HeldStubs.of_map(hollow).units if deps.scheduled
                  else frozenset[FnLocator]())
    # Units a run of this fill kept a body for; no later run of the fill
    # rewrites them before the sweep grades them (`withhold_landed`).
    landed_fill: set[FnLocator] = set()
    # Shadow only (`domain.fill_asks`): runs that would re-ask units an
    # earlier run of this fill asked under unchanged evidence. Counted and
    # reported; no run is deferred on it.
    fill_asks = FillAsks()
    reasked: dict[str, tuple[FnLocator, ...]] = {}
    reasked_minutes = 0.0
    i = 0
    # The schedule's holds and what the executor did to their holders: a
    # held run whose every holder this executor deferred is released
    # (`fill_schedule.release_held`) at the cursor, in schedule order.
    held = [h for h in sched.deferred if h.held_by]
    not_opened: set[str] = set()
    released: set[str] = set()

    def _flush(new_rows: list[RouteLedgerRow]) -> None:
        if deps.ledger is not None and new_rows:
            deps.ledger.append(deps.project, new_rows)

    def _release(keys: set[str]) -> None:
        nonlocal ordered
        not_opened.update(keys)
        freed = release_held(
            [h for h in held if h.run.key not in released], not_opened)
        if not freed:
            return
        released.update(h.run.key for h in freed)
        ordered = ordered[:i] + sorted([*ordered[i:], *freed],
                                       key=lambda r: r.position)
        labels = {h.run.key: h.run.label for h in sched.ordered}
        for h in freed:
            deps.log(
                f"  [phase-c/schedule] fill {deps.iteration}: {h.run.label} "
                f"released — the run(s) its hold waited on did not open "
                f"this fill ({names_of(sorted(labels.get(k, k) for k in h.held_by))}); "
                f"ranked {h.reason} again")
        deps.emit("phase_c.fill.hold_released", data={
            "cluster": deps.cluster, "iter": deps.iteration,
            "released": [h.run.label for h in freed],
            "holders": sorted({labels.get(k, k) for h in freed
                               for k in h.held_by})})
    while i < len(ordered):
        s = ordered[i]
        route = by_key[s.run.plan_key]
        # A wave: this run plus the consecutive runs of the same route when
        # the route allows concurrency. One reading brackets the wave; each
        # run keeps its own clock and share.
        wave = [s]
        if route.parallel:
            # Never two runs over one file in a wave: a file route's runs are
            # one per file as planned, but two carved pieces of one planned
            # run (`carve_rebuys`) share its file and would write it
            # concurrently. The second opens in the next wave.
            wave_files = set(s.run.files)
            j = i + 1
            while (j < len(ordered) and ordered[j].run.kind is s.run.kind
                   and by_key[ordered[j].run.plan_key] is route
                   and not wave_files & set(ordered[j].run.files)):
                wave.append(ordered[j])
                wave_files |= set(ordered[j].run.files)
                j += 1
        i += len(wave)
        deps.check_stop("fill schedule dispatch")
        started = datetime.now(UTC).isoformat(timespec="seconds")
        left = budget.remaining()
        if left is not None and left <= 0.0:
            for w in wave:
                row = replace(
                    _row_for(w, None, None, deps=deps, started=started,
                             minutes=0.0, share=0.0, outcome=RunOutcome.DEFERRED),
                    reason="budget_spent")
                rows.append(row)
                _log_row(deps, row)
            _flush(rows[-len(wave):])
            _release({w.run.key for w in wave})
            continue
        if landed_fill and deps.scheduled:
            wave, withheld_rows, withheld_keys = _withhold_landed_wave(
                wave, landed_fill, deps=deps, started=started)
            rows.extend(withheld_rows)
            _flush(withheld_rows)
            if withheld_keys:
                _release(withheld_keys)
            if not wave:
                continue
        # A run every target of which an earlier run of this fill asked
        # under evidence nothing has moved since asks the same question
        # again. Measured 2026-10-17 over every shadowed run since the
        # shadow shipped (2026-10-13): 15 runs, all impl or per-fn, 175
        # minutes, 5 flat, 3 reverted, 6 empty, none paid a test. They are
        # deferred. A session's question is its slice and its turns, not its
        # units; it stays in the shadow count.
        _reasked_deferred: list[RouteLedgerRow] = []
        _kept_wave: list[ScheduledRun] = []
        for w in wave:
            _tg = tuple(sorted(w.run.targets))
            if not fill_asks.would_defer(_tg):
                _kept_wave.append(w)
                continue
            reasked[w.run.label] = _tg
            if deps.defer_reasked and w.run.kind is not RouteKind.SESSION:
                deps.log(
                    f"  [phase-c/schedule] fill {deps.iteration}: "
                    f"{w.run.label} DEFERRED asked_this_fill — it re-asks "
                    + ", ".join(f"{a}::{b}" for a, b in _tg[:6])
                    + " under evidence no run of this fill has moved")
                row = replace(
                    _row_for(w, None, None, deps=deps, started=started,
                             minutes=0.0, share=0.0,
                             outcome=RunOutcome.DEFERRED),
                    reason="asked_this_fill")
                _reasked_deferred.append(row)
                _log_row(deps, row)
                continue
            _kept_wave.append(w)
            deps.log(
                f"  [phase-c/schedule] fill {deps.iteration}: SHADOW "
                f"asked_this_fill — {w.run.label} re-asks "
                + ", ".join(f"{a}::{b}" for a, b in _tg[:6])
                + " under evidence no run of this fill has moved "
                "(a session, or the deferral is off)")
        if _reasked_deferred:
            rows.extend(_reasked_deferred)
            _flush(_reasked_deferred)
            _release({w.run.key for w in wave if w not in _kept_wave})
            wave = _kept_wave
            if not wave:
                continue
        clocks: dict[str, RunClock] = {}
        shares: dict[str, float | None] = {}
        # The plan reserved later floors against the whole budget. Opening
        # on a weight split of only the time left spends a floor that has
        # not opened yet. Same allotment, on what remains, this wave plus
        # every run still ahead.
        ahead = [*wave, *ordered[i:]]
        granted = budget.opening_shares(ahead)
        for w, share in zip(wave, granted[:len(wave)], strict=True):
            shares[w.run.key] = share
            clocks[w.run.key] = RunClock(
                share_minutes=share, fill_remaining=budget.remaining,
                now=deps.now, ground_moved=gate,
                scope=w.run.target_tests or None, units=w.run.targets,
                unit_multiple=deps.unit_share_multiple,
                call_price=call_price([*prior, *rows], w.run.kind, deps.oracle_fp),
                owed=held_units & w.run.targets)
        mark = kept_mark()
        t0 = deps.now()
        reports: dict[str, RouteRunReport] = {}
        starts: dict[str, float] = {}
        ends: dict[str, float] = {}
        before: dict[str, str] = {}

        def _run_one(w: ScheduledRun, *, _route: FillRoute = route,
                     _starts: dict[str, float] = starts,
                     _ends: dict[str, float] = ends,
                     _reports: dict[str, RouteRunReport] = reports,
                     _clocks: dict[str, RunClock] = clocks,
                     _before: dict[str, str] = before) -> None:
            _starts[w.run.key] = deps.now()
            # The targets' bodies as THIS run opens on them — not the plan's
            # sha, which an earlier run of the same fill may have moved.
            _before[w.run.key] = bodies_sha(w.run.targets)
            with route_scope(w.run.kind.value, w.run.label,
                             cluster=deps.cluster, iteration=deps.iteration):
                # The stream rows of the calls THIS run makes, so a run
                # whose every fill reply was cut mid-deliberation is graded
                # UNMEASURED rather than FLAT. The observer is bound here
                # and not per route because every route reaches the model
                # through the same client; a pooled route's workers see it
                # because `run_units_concurrently` continues this context.
                with observe_call() as _rows:
                    try:
                        _rep = _route.run(w.run, _clocks[w.run.key])
                    finally:
                        # The last claimed unit's bound ends with the run,
                        # so no later loop on this thread (the next run, a
                        # post-merge repair) inherits an expired clock.
                        unit_clock.clear()
                if not _rep.cap_owned and run_all_replies_capped(_rows):
                    _rep = replace(_rep, unmeasured=True)
                _reports[w.run.key] = _landed_nothing(
                    _rep, w.run.targets, _before[w.run.key], bodies_sha)
            _ends[w.run.key] = deps.now()

        try:
            if len(wave) == 1:
                _run_one(wave[0])
            else:
                run_units_concurrently(
                    wave, workers=max(1, deps.parallelism),
                    name_prefix=f"fill-{s.run.kind.value}-{deps.cluster}",
                    run_unit=_run_one, describe=lambda w: w.run.label,
                    log=deps.log, check_stop=deps.check_stop)
        except SystemExit:
            stopped = True
            for w in wave:
                rep = reports.get(w.run.key) or RouteRunReport(stopped=True)
                minutes = (ends.get(w.run.key, deps.now())
                           - starts.get(w.run.key, t0)) / 60.0
                row = _row_for(w, rep, None, deps=deps, started=started,
                               minutes=minutes, share=shares[w.run.key],
                               outcome=RunOutcome.STOPPED)
                rows.append(row)
                _log_row(deps, row)
            _flush(rows[-len(wave):])
            # The cut wave's keeps are on the tree; record them before the
            # stop unwinds past the post-fill write.
            _notify_wave(deps, [reports.get(w.run.key) for w in wave])
            raise
        for w in wave:
            rep = reports.get(w.run.key) or RouteRunReport(empty=True)
            minutes = (ends.get(w.run.key, deps.now())
                       - starts.get(w.run.key, t0)) / 60.0
            # What THIS run kept since the wave opened: keeps are labelled
            # with the run that made them (a single run also collects the
            # unlabelled keeps of a route that could not bind its label).
            kept = kept_since(mark, w.run.label if len(wave) > 1 else None)
            row = _row_for(w, rep, kept, deps=deps, started=started,
                           minutes=minutes, share=shares[w.run.key])
            rows.append(row)
            _log_row(deps, row, unchanged=rep.unchanged)
            if w.run.label in reasked:
                reasked_minutes += row.minutes
            fill_asks.note(row, ((u.file, u.fn_name) for u in rep.dispatched))
        _flush(rows[-len(wave):])
        _notify_wave(deps, [reports.get(w.run.key) for w in wave],
                     rows[-len(wave):])
        for w in wave:
            rep_w = reports.get(w.run.key)
            if rep_w is not None:
                landed_fill |= rep_w.kept
        # A wave can end without its routes asking the clock again after
        # their last keep; ask here, per run still ahead, so no run is opened
        # on a plan whose ground moved under it. Runs planned on tests that
        # did not move stay on the plan.
        # A run the release below puts back at the cursor is asked the
        # same question before it opens; the loop ends when a pass defers
        # nothing (each pass removes at least one run from the plan).
        while i < len(ordered) and gate.armed:
            rest = [w for w in ordered[i:]
                    if gate(w.run.target_tests or None, w.run.targets)]
            if not rest:
                break
            deps.log(
                f"  [phase-c/schedule] fill {deps.iteration}: premise "
                f"moved after {s.run.label} — {len(gate.moved)} test(s) "
                f"no longer fail where the plan read them "
                f"({names_of(gate.moved)}); {len(rest)} run(s) planned "
                f"on them deferred for the sweep to re-plan, "
                f"{len(ordered) - i - len(rest)} run(s) continue")
            deps.emit("phase_c.fill.premise_moved", data={
                "cluster": deps.cluster, "iter": deps.iteration,
                "after": s.run.label, "moved": list(gate.moved[:50]),
                "moved_count": len(gate.moved),
                "deferred": [w.run.label for w in rest],
                "continuing": len(ordered) - i - len(rest)})
            started = datetime.now(UTC).isoformat(timespec="seconds")
            for w in rest:
                row = replace(
                    _row_for(w, None, None, deps=deps, started=started,
                             minutes=0.0, share=0.0,
                             outcome=RunOutcome.DEFERRED),
                    reason="premise_moved")
                rows.append(row)
                _log_row(deps, row)
            _flush(rows[-len(rest):])
            gone = {id(w) for w in rest}
            ordered = ordered[:i] + [w for w in ordered[i:]
                                     if id(w) not in gone]
            _release({w.run.key for w in rest})
    deferred_rows: list[RouteLedgerRow] = []
    for s in sched.deferred:
        if s.run.key in released:
            continue
        row = _row_for(s, None, None, deps=deps,
                       started=datetime.now(UTC).isoformat(timespec="seconds"),
                       minutes=0.0, share=0.0, outcome=RunOutcome.DEFERRED)
        rows.append(row)
        deferred_rows.append(row)
        _log_row(deps, row)
    _flush(deferred_rows)
    if reasked:
        deps.log(
            f"  [phase-c/schedule] fill {deps.iteration}: SHADOW "
            f"asked_this_fill — {len(reasked)} run(s), {reasked_minutes:.1f} "
            "min, re-asked units under unchanged evidence: "
            + ", ".join(sorted(reasked)[:8]))
        deps.emit("phase_c.fill.asked_again", data={
            "cluster": deps.cluster, "iter": deps.iteration,
            "runs": {k: [f"{a}::{b}" for a, b in v]
                     for k, v in sorted(reasked.items())},
            "minutes": round(reasked_minutes, 1)})
    return FillExecution(schedule=sched, rows=tuple(rows), stopped=stopped,
                         premise_moved=gate.moved)

