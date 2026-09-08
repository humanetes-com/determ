"""The file-first route: one run per file of the frontier."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport, RouteSnap
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator, ImplBlock


class FileRoute:
    """File-first: one run per file of the frontier."""

    kind = RouteKind.FILE
    parallel = True

    def __init__(self, *, units_now: Callable[[], list[tuple[str, list[ImplBlock]]]],
                 fill_file: Callable[..., None],
                 admits: Callable[[str, list[ImplBlock]], bool],
                 snapshot: Callable[[], RouteSnap],
                 report_since: Callable[[RouteSnap], RouteRunReport],
                 tests_for: Callable[[Iterable[FnLocator]], frozenset[str]],
                 stubs: frozenset[FnLocator],
                 on_plan: Callable[[list[tuple[str, list[ImplBlock]]]], None],
                 log: Callable[[str], None]) -> None:
        self._units_now = units_now
        self._fill_file = fill_file
        self._admits = admits
        self._snapshot = snapshot
        self._report_since = report_since
        self._tests_for = tests_for
        self._stubs = stubs
        self._on_plan = on_plan
        self._log = log

    def plan(self) -> Sequence[RouteRun]:
        units = self._units_now()
        self._on_plan(units)
        out: list[RouteRun] = []
        # A file the route cannot open under the fn bound is not a run: the
        # schedule ranks only what can be served, so a permanent EMPTY row
        # never heads the order (`_rank_of` skips EMPTY, and would rank the
        # same identity never-served on every fill).
        for i, (file_rel, fblocks) in enumerate(
                (f, bs) for f, bs in units if self._admits(f, bs)):
            locs = frozenset(
                FnLocator(file_rel, m.fn_name) for b in fblocks
                for m in (*b.stub_members, *b.refill_members))
            stub_locs = frozenset(
                FnLocator(file_rel, m.fn_name) for b in fblocks
                for m in b.stub_members) | (locs & self._stubs)
            out.append(RouteRun(
                kind=self.kind, label=f"file {file_rel}", targets=locs,
                target_tests=self._tests_for(locs),
                stubs_in_targets=stub_locs, files=(file_rel,), seq=i))
        return out

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        file_rel = run.files[0] if run.files else ""
        unit = next(((f, bs) for f, bs in self._units_now() if f == file_rel), None)
        if unit is None:
            return RouteRunReport(empty=True)
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        snap = self._snapshot()
        try:
            self._fill_file(unit[0], unit[1], may_claim=clock.may_claim)
        except Exception as exc:  # noqa: BLE001 — a route reports, never dies
            self._log(f"  [phase-c/file] worker error on {file_rel}: {exc}")
        return self._report_since(snap)
