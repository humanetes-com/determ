"""The sweep's revert of landings graded REFUTED on an earlier position.

Two defects of the block this replaces, both measured on run
`273278-22529e44` (2026-09-29, cluster `stable`):

* blame by attribution: every landing whose attributed set touched a moved
  test was reverted, `src/lib.rs::Ok` with 12 gained tests among them. Here
  the candidates are localized first (`cause_bisection.localize`): only the
  rows whose undo moves a position back are reverted.
* the revert was judged by nothing: the fill's readings stood at 37, the
  re-sweep after the revert read 19, and no comparison of the two existed.
  Here the revert is a candidate change judged by the same outcome rule as
  every keep (`TestOutcomes.refuses`) with the pre-revert sweep as its
  baseline, plus the pass count already in hand. A refused revert is rolled
  back byte for byte, and the caller writes no ledger row for it, restores
  every variable it read off the re-sweep, and keeps the coverage binding:
  the tree is the one the pre-revert reading described.

Every probe and the revert itself leave the tree either as they found it or
as the final decision says; nothing here writes the ledger.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.assertion_position import (
    PositionMove,
    TestOutcomes,
    compare_positions,
    outcomes_of,
    positions_for,
)
from pipeline_core.domain.body_ledger import LedgerEntry, SweepView
from pipeline_core.domain.cause_bisection import localize


@dataclass(frozen=True, slots=True)
class SiteSweep:
    """One re-sweep as the revert reads it: the raw capture (so the caller
    can rebuild its own variables from it), the pass count the score uses,
    and the ledger's view of it."""

    rc: int
    output: str
    pass_count: int
    view: SweepView


@dataclass(frozen=True, slots=True)
class SiteRevertPorts:
    #: Put back `rows`' `prior_bodies` in one transaction; returns
    #: (restored rows, reason) — `body_reseed.restore_prior`.
    restore: Callable[[Sequence[LedgerEntry]], tuple[list[LedgerEntry], str]]
    #: Byte snapshot of the source tree, and its restore.
    snapshot: Callable[[], Any]
    rollback: Callable[[Any], object]
    #: Run the full suite on the tree as it stands.
    sweep: Callable[[], SiteSweep]
    log: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SiteRevertResult:
    """What the revert did. `reverted` empty means the tree is the pre-revert
    tree (nothing restorable, refused, or rolled back) and `final` is None;
    otherwise `final` is the re-sweep of the reverted tree."""

    candidates: tuple[LedgerEntry, ...]
    reverted: tuple[LedgerEntry, ...] = ()
    final: SiteSweep | None = None
    rolled_back: bool = False
    judged: TestOutcomes | None = None
    why: str = ""
    probes: int = 0


def moved_earlier(rows: Sequence[LedgerEntry], before: SweepView,
                  after: SweepView) -> tuple[PositionMove, ...]:
    """The position moves the rows' `regressed` deltas name, by key."""
    tests = {t for r in rows for t, k in r.verdict_delta if k == "regressed"}
    earlier, _later = compare_positions(positions_for(before.positions, tests),
                                        positions_for(after.positions, tests))
    return earlier


def _moved_back(moves: Sequence[PositionMove], view: SweepView) -> bool:
    """Some move's test is green again, or stands at or past its position
    before the landing."""
    for m in moves:
        if m.key in view.keyed.passed and m.key not in view.keyed.failed:
            return True
        now = view.positions.get(m.key)
        if now is not None and now >= m.before:
            return True
    return False


def judge_revert(pre: SiteSweep, after: SiteSweep) -> tuple[bool, TestOutcomes]:
    """Whether a revert stands: the decided rule over every test the
    pre-revert sweep judged, with that sweep as the baseline, and no drop
    of the pass count in hand before it."""
    outcomes = outcomes_of(pre.view.ran, before=pre.view.keyed,
                           after=after.view.keyed)
    return (not outcomes.refuses and after.pass_count >= pre.pass_count,
            outcomes)


def revert_site_regressions(
    ports: SiteRevertPorts, *, rows: Sequence[LedgerEntry],
    before: SweepView, pre: SiteSweep,
) -> SiteRevertResult:
    """Localize, revert, judge, and roll back a refused revert.

    `rows` are the landings graded REFUTED with `regressed` deltas against
    `before` (the sweep before their landing) and `pre` (this sweep)."""
    cands = tuple(r for r in rows if r.prior_bodies)
    if not cands:
        return SiteRevertResult(tuple(rows),
                                why="no row carries the bodies its landing "
                                    "replaced")
    moves = moved_earlier(cands, before, pre.view)
    base = ports.snapshot()
    probes = 0

    def _probe(subset: Sequence[LedgerEntry]) -> bool:
        nonlocal probes
        restored, _why = ports.restore(subset)
        if not restored:
            ports.rollback(base)
            return False
        probes += 1
        try:
            return _moved_back(moves, ports.sweep().view)
        finally:
            ports.rollback(base)

    causal = localize(cands, _probe) if moves else cands
    restored, why = ports.restore(causal)
    if not restored:
        ports.rollback(base)
        return SiteRevertResult(cands, why=why, probes=probes)
    final = ports.sweep()
    stands, outcomes = judge_revert(pre, final)
    if not stands:
        ports.rollback(base)
        lost = pre.pass_count - final.pass_count
        ports.log(
            f"site revert rolled back — the restored tree loses "
            f"{max(lost, len(outcomes.lost))} passing test(s) "
            f"({final.pass_count} against {pre.pass_count} before the "
            f"revert"
            + (f"; lost {', '.join(sorted(outcomes.lost)[:6])}"
               if outcomes.lost else "")
            + (f"; earlier {', '.join(outcomes.earlier_tests[:6])}"
               if outcomes.earlier and not outcomes.gained else "")
            + ")")
        return SiteRevertResult(cands, rolled_back=True, judged=outcomes,
                                why="the revert lost passing tests",
                                probes=probes)
    return SiteRevertResult(cands, reverted=tuple(restored), final=final,
                            judged=outcomes, probes=probes)


__all__ = ["SiteRevertPorts", "SiteRevertResult", "SiteSweep", "judge_revert",
           "moved_earlier", "revert_site_regressions"]
