"""Replay one recorded fill through the fill schedule and executor.

A walk (`tests/walks/harness.py`) replays the TDD LOOP with the fill stubbed
whole; this harness replays the FILL: the candidate runs a recorded fill
would have planned, the hollow relation its leftover tests had, the stubs
it held, and the route ledger it would have read — through the real
`domain.fill_schedule.schedule` and `fill_scheduler.execute_fill`, with the
routes replaced by fakes that record the order they were opened in and
what the clock let them claim.

What it MODELS rather than replays, named here so the list is extended
deliberately: the held view (`hollow_of`): recorded fills carry no
first-failure holds, so a test is taken as held by every stub on its
recorded slice, which is the wider reading the pipeline no longer uses and
the only one these records allow; the candidate runs of the unit routes (one file run per file
that held a stub, targets = that file's sites in the recorded slices; one
per-fn run over the stubs) — pre-fix events carry no route plan, so this
is `distill_fill.py`'s derivation, not a record; the stub inventory of
pre-fix fills (`stubs_source: "argument"`), checked against the plan
event's hollow count by the recorded test; the attempt sequence of a route
run (`route_attempts`, distilled from the fill's revert / done / route-row
events — the distiller's header says which names are the record and which
are derived), replayed through the real `domain.fill_attempts` bookkeeping
the routes share, so the row comes out of the same `since` reading the
loop takes; and time, which a fake route does not spend.

What a replay may assert: an ORDER became reachable (the stub run before
the session), a run was deferred, an unmeasurable run was deferred, the
executor recorded rows, a row's outcome / kept / reverted for a run whose
attempts are recorded. Never a score.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecution,
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.domain.fill_attempts import FillAttempts, current_run_label
from pipeline_core.domain.fill_schedule import (
    TODAY_ORDER,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
)
from pipeline_core.domain.models import FnLocator, FnSite

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fills"


def load_fill(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def slices_of(fix: Mapping[str, Any]) -> dict[str, frozenset[FnSite]]:
    return {
        t: frozenset(FnSite(s["file"], s["fn"], s.get("container", ""))
                     for s in sites)
        for t, sites in dict(fix["tests"]).items()}


def stubs_of(fix: Mapping[str, Any]) -> frozenset[FnLocator]:
    out: set[FnLocator] = set()
    for loc in fix.get("stubs") or []:
        f, _, fn = str(loc).partition("::")
        out.add(FnLocator(f, fn))
    return frozenset(out)


def hollow_of(fix: Mapping[str, Any]) -> dict[str, frozenset[FnLocator]]:
    """The modeled held view: test -> the stubs on its recorded slice."""
    stubs = stubs_of(fix)
    out: dict[str, frozenset[FnLocator]] = {}
    for t, sites in slices_of(fix).items():
        hit = frozenset(FnLocator(x.file, x.fn_name) for x in sites
                        if FnLocator(x.file, x.fn_name) in stubs)
        if hit:
            out[t] = hit
    return out


def _loc(label: str) -> FnLocator:
    f, _, fn = label.partition("::")
    return FnLocator(f, fn)


def _run_from_schedule(raw: Mapping[str, Any], seq: int) -> RouteRun:
    kind = RouteKind(str(raw["kind"]))
    targets = frozenset(_loc(x) for x in raw.get("targets") or [])
    stubs = frozenset(_loc(x) for x in raw.get("stubs") or [])
    return RouteRun(
        kind=kind, label=str(raw.get("label", "")), targets=targets,
        target_tests=frozenset(str(t) for t in raw.get("target_tests") or []),
        stubs_in_targets=stubs,
        files=tuple(str(f) for f in raw.get("files") or ()),
        seq=int(raw.get("seq", seq) or 0),
        bodies_sha=str(raw.get("bodies_sha", "")))


def ledger_of(fix: Mapping[str, Any]) -> list[RouteLedgerRow]:
    out: list[RouteLedgerRow] = []
    for raw in fix.get("ledger_rows") or []:
        row = RouteLedgerRow.from_dict(raw)
        if row is not None:
            out.append(row)
    return out


def unmeasurable_of(fix: Mapping[str, Any]) -> frozenset[str]:
    """Failed names that have no completed-binary pair — the oracle's
    incomplete list as test names."""
    measured = {str(p[1]).rsplit("::", 1)[-1]
                for p in (fix.get("failed_pairs") or []) if len(p) >= 2}
    return frozenset(
        str(n).rsplit("::", 1)[-1] for n in (fix.get("failed") or [])
        if str(n).rsplit("::", 1)[-1] not in measured)


def recorded_held_of(fix: Mapping[str, Any]) -> dict[str, frozenset[FnLocator]]:
    """The held view the schedule itself read (`schedule_inputs.hollow`),
    not the slice reading `hollow_of` rebuilds."""
    raw = dict((fix.get("schedule_inputs") or {}).get("hollow") or {})
    return {t: frozenset(_loc(u) for u in us) for t, us in raw.items()}


def recorded_blame_of(fix: Mapping[str, Any]) -> dict[FnLocator, frozenset[str]]:
    """The crash blame the schedule itself read, with the tests each culprit
    crashed when the event carried them."""
    inputs = fix.get("schedule_inputs") or {}
    tests = dict(inputs.get("crash_blamed_tests") or {})
    return {_loc(u): frozenset(tests.get(u) or ())
            for u in inputs.get("crash_blamed") or ()}


def crash_blamed_of(fix: Mapping[str, Any]) -> frozenset[FnLocator]:
    return frozenset(
        FnLocator(str(a), str(b)) for a, b in (fix.get("crash_blamed") or []))


def candidate_runs(fix: Mapping[str, Any]) -> dict[RouteKind, list[RouteRun]]:
    """The runs each route would have planned on the recorded fill.

    When the fixture carries `schedule_runs` (the schedule event's
    candidates), those are the record and this returns them grouped by
    kind. Pre-fix fixtures derive file/per-fn runs from slices and stubs.
    """
    if fix.get("schedule_runs"):
        by_kind: dict[RouteKind, list[RouteRun]] = {k: [] for k in RouteKind}
        for i, raw in enumerate(fix["schedule_runs"]):
            run = _run_from_schedule(raw, i)
            by_kind.setdefault(run.kind, []).append(run)
        return by_kind
    slices = slices_of(fix)
    stubs = stubs_of(fix)
    by_kind = {
        RouteKind.SESSION: [], RouteKind.FILE: [], RouteKind.PER_FN: []}
    for i, s in enumerate(fix.get("sessions") or []):
        tests = tuple(s["tests"])
        units: set[FnLocator] = set()
        for t in tests:
            units.update(FnLocator(u.file, u.fn_name) for u in slices.get(t, ()))
        by_kind[RouteKind.SESSION].append(RouteRun(
            kind=RouteKind.SESSION,
            label=(f"session[{len(tests)} test(s), "
                   f"{int(s.get('units', len(units)))} unit(s)] #{i}"),
            targets=frozenset(units), target_tests=frozenset(tests),
            files=tuple(sorted({u.file for u in units})), seq=i, bodies_sha="s"))
    for i, fr in enumerate(fix.get("file_runs") or []):
        targets = frozenset(_loc(x) for x in fr["targets"])
        f_stubs = frozenset(_loc(x) for x in fr.get("stubs") or []) | (targets & stubs)
        tests = frozenset(t for t, us in slices.items()
                          if any(FnLocator(u.file, u.fn_name) in targets for u in us))
        by_kind[RouteKind.FILE].append(RouteRun(
            kind=RouteKind.FILE, label=f"file {fr['file']}", targets=targets,
            target_tests=tests, stubs_in_targets=f_stubs,
            files=(str(fr["file"]),), seq=i, bodies_sha="f"))
    if stubs:
        tests = frozenset(t for t, us in slices.items()
                          if any(FnLocator(u.file, u.fn_name) in stubs for u in us))
        by_kind[RouteKind.PER_FN].append(RouteRun(
            kind=RouteKind.PER_FN, label=f"per-fn {len(stubs)} stub(s)",
            targets=stubs, target_tests=tests, stubs_in_targets=stubs,
            files=tuple(sorted({s.file for s in stubs})), bodies_sha="p"))
    return by_kind


@dataclass(frozen=True)
class RecordedAttempts:
    """One route run's recorded attempt sequence: the units it dispatched
    and, per attempt, the outcome and the units it applied to."""

    dispatched: frozenset[FnLocator]
    attempts: tuple[tuple[str, frozenset[FnLocator]], ...]


def route_attempts_of(fix: Mapping[str, Any]) -> dict[str, RecordedAttempts]:
    out: dict[str, RecordedAttempts] = {}
    for raw in fix.get("route_attempts") or []:
        out[str(raw["label"])] = RecordedAttempts(
            dispatched=frozenset(_loc(x) for x in raw.get("dispatched") or []),
            attempts=tuple(
                (str(a["outcome"]), frozenset(_loc(x) for x in a.get("units") or []))
                for a in raw.get("attempts") or []))
    return out


class FakeRoute:
    """A route whose run replays the recorded attempt sequence through the
    real attempt bookkeeping. Notes are stamped with the run label the
    executor binds on the running thread (the loop's own stamping); the
    row is read with `since(..., label)` as `impl_fill.prepare_impl_fill`'s `_report_since`
    does, or — `labelled=False`, the pre-fix reading — off the whole tick
    window, the negative control of the wave-attribution sentence."""

    def __init__(self, kind: RouteKind, runs: Sequence[RouteRun], opened: list[str],
                 *, parallel: bool = False,
                 attempts: Mapping[str, RecordedAttempts] | None = None,
                 ledger: FillAttempts | None = None,
                 labelled: bool = True) -> None:
        self.kind = kind
        self.parallel = parallel
        self._runs = list(runs)
        self._opened = opened
        self._attempts = dict(attempts or {})
        self._ledger = ledger if ledger is not None else FillAttempts()
        self._labelled = labelled

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        self._opened.append(run.label)
        rec = self._attempts.get(run.label)
        if rec is None:
            return RouteRunReport(model_calls=1, kept=run.targets)
        # The loop's own reading: snapshot, dispatch, one note per attempt
        # (`impl_fill`'s keep / revert / lost sites), one call per attempt,
        # `since` at the end.
        before = self._ledger.snapshot()
        self._ledger.note_dispatched(rec.dispatched)
        for outcome, units in rec.attempts:
            self._ledger.note_call()
            {"kept": self._ledger.note_kept,
             "reverted": self._ledger.note_reverted,
             "lost": self._ledger.note_lost}[outcome](units)
        delta = FillAttempts.since(
            before, self._ledger.snapshot(),
            current_run_label() if self._labelled else None)
        return RouteRunReport.from_delta(delta, {})


class MemoryLedger:
    def __init__(self, rows: Sequence[RouteLedgerRow] = ()) -> None:
        self.rows = list(rows)

    def load(self, project: str, cluster: str) -> list[RouteLedgerRow]:
        return list(self.rows)

    def append(self, project: str, rows: Sequence[RouteLedgerRow]) -> int:
        self.rows.extend(rows)
        return len(rows)


def replay(fix: Mapping[str, Any], *, scheduled: bool = True,
           budget: float = 0.0, rows: Sequence[RouteLedgerRow] = (),
           opened: list[str] | None = None,
           log: list[str] | None = None,
           unmeasurable_tests: Sequence[str] = (),
           crash_blamed: Sequence[FnLocator] | Mapping[FnLocator, frozenset[str]] = (),
           hollow: Mapping[str, frozenset[FnLocator]] | None = None,
           oracle_fp: str = "o",
           sticky_reverts: bool = False,
           labelled: bool = True) -> tuple[FillExecution, list[str]]:
    """Run the recorded fill's candidates through the executor. Returns the
    execution and the labels in the order the routes were opened.
    `sticky_reverts=True` replays recorded attempts under the pre-fix
    bookkeeping (a revert mark no keep clears); `labelled=False` reads
    each row off the whole tick window rather than the run's own label —
    each the negative control of its sentence."""
    opened = [] if opened is None else opened
    log = [] if log is None else log
    by_kind = candidate_runs(fix)
    _parallel = {RouteKind.FILE, RouteKind.IMPL, RouteKind.PAIR}
    attempts = route_attempts_of(fix)
    ledger = FillAttempts(sticky_reverts=sticky_reverts)
    routes = []
    for kind in TODAY_ORDER:
        planned = by_kind.get(kind) or []
        if planned:
            routes.append(FakeRoute(
                kind, planned, opened, parallel=kind in _parallel,
                attempts=attempts, ledger=ledger, labelled=labelled))
    deps = FillExecutionDeps(
        log=log.append, emit=lambda *_a, **_k: None, check_stop=lambda _c: None,
        ledger=MemoryLedger(rows), project="anyhow",
        cluster=str(fix.get("cluster", "stable")), run_id=str(fix["run_id"]),
        iteration=int(fix["iter"]), oracle_fp=oracle_fp, budget_minutes=budget,
        scheduled=scheduled, parallelism=1,
        unmeasurable_tests=frozenset(unmeasurable_tests),
        crash_blamed=(dict(crash_blamed) if isinstance(crash_blamed, Mapping)
                      else frozenset(crash_blamed)))
    out = execute_fill(routes,
                       hollow=hollow_of(fix) if hollow is None else hollow,
                       bodies_sha=lambda _t: "b", deps=deps)
    return out, opened
