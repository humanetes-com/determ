"""Replay one recorded fill through the fill schedule and executor.

A walk (`tests/walks/harness.py`) replays the TDD LOOP with the fill stubbed
whole; this harness replays the FILL: the candidate runs a recorded fill
would have planned, the hollow relation its leftover tests had, the stubs
it held, and the route ledger it would have read — through the real
`domain.fill_schedule.schedule` and `fill_scheduler.execute_fill`, with the
routes replaced by fakes that record the order they were opened in and
what the clock let them claim.

What it MODELS rather than replays, named here so the list is extended
deliberately: the candidate runs of the unit routes (one file run per file
that held a stub, targets = that file's sites in the recorded slices; one
per-fn run over the stubs) — pre-fix events carry no route plan, so this
is `distill_fill.py`'s derivation, not a record; the stub inventory of
pre-fix fills (`stubs_source: "argument"`), checked against the plan
event's hollow count by the recorded test; and time, which a fake route
does not spend.

What a replay may assert: an ORDER became reachable (the stub run before
the session), a run was deferred, the executor recorded rows. Never a
score.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecution,
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    hollow_by_test,
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
    return hollow_by_test(slices_of(fix), stubs_of(fix))


def _loc(label: str) -> FnLocator:
    f, _, fn = label.partition("::")
    return FnLocator(f, fn)


def candidate_runs(fix: Mapping[str, Any]) -> dict[RouteKind, list[RouteRun]]:
    """The runs each route would have planned on the recorded fill."""
    slices = slices_of(fix)
    stubs = stubs_of(fix)
    by_kind: dict[RouteKind, list[RouteRun]] = {
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


class FakeRoute:
    def __init__(self, kind: RouteKind, runs: Sequence[RouteRun], opened: list[str],
                 *, parallel: bool = False) -> None:
        self.kind = kind
        self.parallel = parallel
        self._runs = list(runs)
        self._opened = opened

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        self._opened.append(run.label)
        return RouteRunReport(model_calls=1, kept=run.targets)


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
           log: list[str] | None = None) -> tuple[FillExecution, list[str]]:
    """Run the recorded fill's candidates through the executor. Returns the
    execution and the labels in the order the routes were opened."""
    opened = [] if opened is None else opened
    log = [] if log is None else log
    by_kind = candidate_runs(fix)
    routes = [
        FakeRoute(RouteKind.SESSION, by_kind[RouteKind.SESSION], opened),
        FakeRoute(RouteKind.FILE, by_kind[RouteKind.FILE], opened, parallel=True),
        FakeRoute(RouteKind.PER_FN, by_kind[RouteKind.PER_FN], opened),
    ]
    deps = FillExecutionDeps(
        log=log.append, emit=lambda *_a, **_k: None, check_stop=lambda _c: None,
        ledger=MemoryLedger(rows), project="anyhow",
        cluster=str(fix.get("cluster", "stable")), run_id=str(fix["run_id"]),
        iteration=int(fix["iter"]), oracle_fp="o", budget_minutes=budget,
        scheduled=scheduled, parallelism=1)
    out = execute_fill(routes, hollow=hollow_of(fix), bodies_sha=lambda _t: "b",
                       deps=deps)
    return out, opened
