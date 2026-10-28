"""The slice-session route: the packed red-slice sessions, one run each."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.slice_session import SessionSlice, slice_for_run


def _bare(name: str) -> str:
    return str(name).rsplit("::", 1)[-1]


class SessionRoute:
    """The packed slice sessions, one run each."""

    kind = RouteKind.SESSION
    parallel = False

    def __init__(self, plans: Sequence[SessionSlice],
                 run_one: Callable[[SessionSlice, RunClock], RouteRunReport],
                 served: set[FnLocator], *, log: Callable[[str], None]) -> None:
        self._plans = list(plans)
        self._run_one = run_one
        self.served = served
        self._log = log

    def plan(self) -> Sequence[RouteRun]:
        return [RouteRun(
            kind=self.kind, label=sl.label(seq=i),
            targets=frozenset(locator_of(u) for u in sl.units),
            target_tests=frozenset(_bare(t) for t in sl.tests),
            files=tuple(sl.files), seq=i, joint=sl.joint)
            for i, sl in enumerate(self._plans)]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        sl = slice_for_run(self._plans, run)
        if sl is None:
            return RouteRunReport(empty=True)
        if run.frozen:
            sl = replace(sl, frozen=frozenset(run.frozen))
        if not clock.may_claim():
            self._log(f"  [phase-c/session] {run.label}: not opened — the "
                      "fill share is spent.")
            return RouteRunReport(empty=True)
        return self._run_one(sl, clock)
