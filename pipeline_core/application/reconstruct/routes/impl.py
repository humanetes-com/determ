"""The per-impl route: one run per file of viable blocks, blocks serial."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport, RouteSnap
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator, ImplBlock


class ImplRoute:
    """Per-impl: one run per file of viable blocks, blocks serial within."""

    kind = RouteKind.IMPL
    parallel = True

    def __init__(self, *, blocks_now: Callable[[], list[ImplBlock]],
                 fill_block: Callable[[ImplBlock], None],
                 snapshot: Callable[[], RouteSnap],
                 report_since: Callable[[RouteSnap], RouteRunReport],
                 tests_for: Callable[[Iterable[FnLocator]], frozenset[str]],
                 stubs: frozenset[FnLocator],
                 log: Callable[[str], None]) -> None:
        self._blocks_now = blocks_now
        self._fill_block = fill_block
        self._snapshot = snapshot
        self._report_since = report_since
        self._tests_for = tests_for
        self._stubs = stubs
        self._log = log

    @staticmethod
    def _by_file(blocks: Sequence[ImplBlock]) -> dict[str, list[ImplBlock]]:
        out: dict[str, list[ImplBlock]] = {}
        for b in blocks:
            out.setdefault(b.file, []).append(b)
        return out

    def plan(self) -> Sequence[RouteRun]:
        out: list[RouteRun] = []
        for i, (file_rel, fblocks) in enumerate(self._by_file(self._blocks_now()).items()):
            locs = frozenset(
                FnLocator(file_rel, m.fn_name) for b in fblocks
                for m in (*b.stub_members, *b.refill_members))
            stub_locs = frozenset(
                FnLocator(file_rel, m.fn_name) for b in fblocks
                for m in b.stub_members) | (locs & self._stubs)
            out.append(RouteRun(
                kind=self.kind, label=f"impl {file_rel}", targets=locs,
                target_tests=self._tests_for(locs),
                stubs_in_targets=stub_locs, files=(file_rel,), seq=i))
        return out

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        file_rel = run.files[0] if run.files else ""
        fblocks = self._by_file(self._blocks_now()).get(file_rel) or []
        if not fblocks:
            return RouteRunReport(empty=True)
        snap = self._snapshot()
        opened = 0
        for blk in fblocks:
            if not clock.may_claim():
                self._log(f"  [phase-c/impl] {file_rel}: fill share spent after "
                          f"{opened} of {len(fblocks)} block(s) — the rest are "
                          "left to the next fill.")
                break
            opened += 1
            try:
                self._fill_block(blk)
            except Exception as _we:  # noqa: BLE001 — a route reports, never dies
                self._log(f"  [phase-c/impl] worker error on "
                          f"{blk.file}::{blk.container}: {_we}")
        if opened == 0:
            return RouteRunReport(empty=True)
        return self._report_since(snap)
