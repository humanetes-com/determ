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

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from pipeline_core.application.reconstruct.fill_reading import (
    begin_fill,
    kept_mark,
    kept_since,
    unmeasurable_tests_now,
)
from pipeline_core.application.reconstruct.fill_routes import (
    FillRoute,
    RouteLedger,
    RouteRunReport,
)
from pipeline_core.application.reconstruct.unit_pool import run_units_concurrently
from pipeline_core.domain.fill_context import route_scope
from pipeline_core.domain.fill_schedule import (
    FillBudget,
    FillSchedule,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
    ScheduledRun,
    run_is_unmeasurable,
    schedule,
)
from pipeline_core.domain.models import FnLocator

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
    crash_blamed: frozenset[FnLocator] = frozenset()
    #: How many runs of a route kind one fill opens (`schedule`'s
    #: ``max_per_kind``); the rest are deferred to the next fill. Empty =
    #: unbounded. The session cap is `SLICE_SESSION_MAX_PER_FILL`.
    max_per_kind: Mapping[RouteKind, int] = field(default_factory=dict)
    #: Locators leftover tests convict before measured-owners admission
    #: (eligible plus hop-pair members). Empty (the default) skips
    #: coverage-split marking, so every recorded fill fixture's order is
    #: unchanged by this argument. Snapshot before owner-map admission
    #: or the spine locators become "attributed".
    attributed: frozenset[FnLocator] = frozenset()
    #: Called once per wave, after the wave's route rows are flushed, with
    #: the units the wave's reports say they kept — and once more on the
    #: wave a graceful stop cut, before the stop propagates. The driver's
    #: unit-ledger writer sits behind it: the per-unit `landed` rows used
    #: to be written only after the whole fill returned, so a fill that
    #: ended on a stop left every kept body without a row (four fills of
    #: 2026-09-08, about sixty bodies, zero rows). The route ledger is
    #: flushed per wave for the same reason; this is the unit ledger's
    #: turn. None (the default) keeps the post-fill write alone.
    on_wave_kept: Callable[[frozenset[FnLocator]], None] | None = None


@dataclass(frozen=True, slots=True)
class FillExecution:
    schedule: FillSchedule
    rows: tuple[RouteLedgerRow, ...]
    stopped: bool = False


def _notify_wave(deps: FillExecutionDeps,
                 reports: Sequence[RouteRunReport | None]) -> None:
    """Hand a finished (or stopped) wave's kept units to the driver's
    unit-ledger writer. A wave whose every report is `empty` opened
    nothing and is not reported; a failure in the writer is logged and
    never ends the fill — the ledger records the fill, it does not run it."""
    if deps.on_wave_kept is None:
        return
    present = [r for r in reports if r is not None]
    if present and all(r.empty for r in present):
        return
    kept = frozenset(u for r in present for u in r.kept)
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
        kept=_pairs(rep.kept), reverted=_pairs(rep.reverted),
        target_tests=tuple(sorted(s.run.target_tests)),
        classes=dict(rep.classes), share_minutes=share, reason=s.reason)


def _log_row(deps: FillExecutionDeps, row: RouteLedgerRow) -> None:
    deps.log(
        f"  [phase-c/route] {row.label}: {row.outcome.value.upper()} after "
        f"{row.minutes:.1f} min ({row.model_calls} call(s), net "
        f"{row.net_pass_delta:+d}, kept {len(row.kept)}, reverted "
        f"{len(row.reverted)}"
        + (f", newly red {len(row.newly_red)}" if row.newly_red else "")
        + (f", crashed {list(row.newly_crashed)}" if row.newly_crashed else "")
        + f"; {row.reason})")
    deps.emit("phase_c.route.run", data=row.to_dict())


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
        attributed=deps.attributed if deps.scheduled else ())
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
        "deferred": [s.to_dict() for s in sched.deferred]})

    budget = FillBudget(sched.budget_minutes, now=deps.now)
    remaining_weight = sum(s.weight for s in sched.ordered)
    rows: list[RouteLedgerRow] = []
    stopped = False
    ordered = list(sched.ordered)
    begin_fill()
    i = 0

    def _flush(new_rows: list[RouteLedgerRow]) -> None:
        if deps.ledger is not None and new_rows:
            deps.ledger.append(deps.project, new_rows)
    while i < len(ordered):
        s = ordered[i]
        route = by_key[s.run.key]
        # A wave: this run plus the consecutive runs of the same route when
        # the route allows concurrency. One reading brackets the wave; each
        # run keeps its own clock and share.
        wave = [s]
        if route.parallel:
            j = i + 1
            while (j < len(ordered) and ordered[j].run.kind is s.run.kind
                   and by_key[ordered[j].run.key] is route):
                wave.append(ordered[j])
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
                remaining_weight -= w.weight
            _flush(rows[-len(wave):])
            continue
        # Fill-time re-grade: a keep earlier in THIS fill made every aimed
        # test's binary abort or hang, and no target is crash-blamed. The
        # budget-0 contract still opens plan-time unmeasurable runs (no
        # reading yet); a keep is the reading. Skip even under budget 0.
        now_unmeas = unmeasurable_tests_now()
        if deps.scheduled and now_unmeas:
            skipped = [w for w in wave
                       if run_is_unmeasurable(
                           w.run, now_unmeas, deps.crash_blamed)]
            kept_wave = [w for w in wave if w not in skipped]
            if skipped:
                for w in skipped:
                    remaining_weight -= w.weight
                    deps.log(
                        f"  [phase-c/schedule] {w.run.label}: re-graded "
                        "rebuy_flat at open — every aimed test is in a "
                        "binary that did not complete at a keep in this fill")
                    row = replace(
                        _row_for(w, None, None, deps=deps, started=started,
                                 minutes=0.0, share=0.0,
                                 outcome=RunOutcome.DEFERRED),
                        reason="rebuy_flat")
                    rows.append(row)
                    _log_row(deps, row)
                _flush(rows[-len(skipped):])
            if not kept_wave:
                continue
            wave = kept_wave
        clocks: dict[str, RunClock] = {}
        shares: dict[str, float | None] = {}
        for w in wave:
            share = budget.share_for(w.weight, remaining_weight)
            remaining_weight -= w.weight
            shares[w.run.key] = share
            clocks[w.run.key] = RunClock(
                share_minutes=share, fill_remaining=budget.remaining,
                now=deps.now)
        mark = kept_mark()
        t0 = deps.now()
        reports: dict[str, RouteRunReport] = {}
        starts: dict[str, float] = {}
        ends: dict[str, float] = {}

        def _run_one(w: ScheduledRun, *, _route: FillRoute = route,
                     _starts: dict[str, float] = starts,
                     _ends: dict[str, float] = ends,
                     _reports: dict[str, RouteRunReport] = reports,
                     _clocks: dict[str, RunClock] = clocks) -> None:
            _starts[w.run.key] = deps.now()
            with route_scope(w.run.kind.value, w.run.label,
                             cluster=deps.cluster, iteration=deps.iteration):
                _reports[w.run.key] = _route.run(w.run, _clocks[w.run.key])
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
            _log_row(deps, row)
        _flush(rows[-len(wave):])
        _notify_wave(deps, [reports.get(w.run.key) for w in wave])
    deferred_rows: list[RouteLedgerRow] = []
    for s in sched.deferred:
        row = _row_for(s, None, None, deps=deps,
                       started=datetime.now(UTC).isoformat(timespec="seconds"),
                       minutes=0.0, share=0.0, outcome=RunOutcome.DEFERRED)
        rows.append(row)
        deferred_rows.append(row)
        _log_row(deps, row)
    _flush(deferred_rows)
    return FillExecution(schedule=sched, rows=tuple(rows), stopped=stopped)

