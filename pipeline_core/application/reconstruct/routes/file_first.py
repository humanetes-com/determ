"""The file-first route: one run per file of the frontier."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport, RouteSnap
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator, ImplBlock
from pipeline_core.domain.refill_stamp import narrow_blocks_to


class FileRoute:
    """File-first: one run per file of the frontier.

    A file is designed in one reply, but its units are ranked one by one:
    the plan names each target with its own tests (`RouteRun.unit_tests`),
    so the schedule can carve a unit whose own landing convicts it out of
    the file (`fill_schedule.carve_rebuys`) instead of deferring the whole
    file on its account. A carved run writes the rest of the file in one
    reply as before and withholds the carved units' bodies.
    """

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
                stubs_in_targets=stub_locs, files=(file_rel,), seq=i,
                unit_tests=tuple(
                    (loc, self._tests_for((loc,)))
                    for loc in sorted(locs, key=lambda k: k.fn_name))))
        return out

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        file_rel = run.files[0] if run.files else ""
        unit = next(((f, bs) for f, bs in self._units_now() if f == file_rel), None)
        if unit is None:
            return RouteRunReport(empty=True)
        blocks, withheld = list(unit[1]), frozenset[str]()
        if run.parent:
            # A carved run: the schedule deferred the rest of the file's
            # units as re-buys this fill.
            blocks, withheld = narrow_blocks_to(unit[1], run.targets)
            if not blocks:
                return RouteRunReport(empty=True)
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        snap = self._snapshot()
        try:
            self._fill_file(unit[0], blocks, may_claim=clock.may_claim,
                            withheld=withheld)
        except Exception as exc:  # noqa: BLE001 — a route reports, never dies
            self._log(f"  [phase-c/file] worker error on {file_rel}: {exc}")
        return self._report_since(snap)
