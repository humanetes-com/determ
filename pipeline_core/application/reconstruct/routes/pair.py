"""The formatter-pair route: dispatcher/implementer pairs, one run each."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport, RouteSnap
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.pair_fill import PairDispatch


class PairRoute:
    """The formatter dispatcher/implementer pairs, one run each."""

    kind = RouteKind.PAIR
    parallel = False

    def __init__(self, jobs: Sequence[PairDispatch], *,
                 fill_pair: Callable[[PairDispatch], None],
                 pair_served: set[FnLocator],
                 session_served: Callable[[], set[FnLocator]],
                 snapshot: Callable[[], RouteSnap],
                 report_since: Callable[[RouteSnap], RouteRunReport],
                 tests_for: Callable[[Iterable[FnLocator]], frozenset[str]],
                 stubs: frozenset[FnLocator],
                 log: Callable[[str], None]) -> None:
        self._jobs = list(jobs)
        self._fill_pair = fill_pair
        self._pair_served = pair_served
        self._session_served = session_served
        self._snapshot = snapshot
        self._report_since = report_since
        self._tests_for = tests_for
        self._stubs = stubs
        self._log = log

    @staticmethod
    def _label(pd: PairDispatch) -> str:
        return "pair " + " + ".join(sorted(pd.member_scope()))

    def plan(self) -> Sequence[RouteRun]:
        out: list[RouteRun] = []
        for i, pd in enumerate(self._jobs):
            locs = pd.member_locators()
            out.append(RouteRun(
                kind=self.kind, label=self._label(pd), targets=locs,
                target_tests=self._tests_for(locs),
                stubs_in_targets=locs & self._stubs,
                files=tuple(sorted({k.file for k in locs})), seq=i))
        return out

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        pd = next((j for j in self._jobs
                   if j.member_locators() == run.targets), None)
        if pd is None:
            return RouteRunReport(empty=True)
        if pd.member_locators() & self._session_served():
            # A session kept or accepted these members under one judgment
            # this call; the next sweep judges that write.
            return RouteRunReport(empty=True)
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        snap = self._snapshot()
        try:
            self._fill_pair(pd)
        except Exception as _pfe:  # noqa: BLE001 — a route reports, never dies
            self._log(f"  [phase-c/pair] worker error: {_pfe}")
        self._pair_served |= pd.member_locators()
        return self._report_since(snap)
