"""What the fill schedule bought, read off the route ledger — pure.

`schedule_invariants` says whether ONE decision is self-consistent. It cannot
say whether the decisions, fill after fill, were spent well: run
`82925-203ed7b8` (2026-09-28) passed every rule and still spent 6 of its first
7 fills with under half of the minutes on runs that paid (fills 5 and 6 spent
150 minutes and cashed 12), because the run that paid, the per-fn route for
one stub, was deferred `coverage_split` in fills 4, 5 and 6 and cashed in
fill 7. This module reads the ledger rows the executor already writes and
names those costs as numbers, so a monitor row, a silent watcher and the
census read one definition.

Definitions, all over one run's rows (`RouteLedgerRow`):

* a fill is the rows sharing an `iteration`; a row that OPENED is any row whose
  outcome is not `deferred`;
* `spent` = minutes of opened rows; `cashed` = minutes of rows whose outcome is
  `cashed` (verdicts moved); `yield_share` = cashed / spent, None when the fill
  spent under `MIN_FILL_MINUTES` (a sub-five-minute fill is not a reading);
* `rebuy_minutes` = minutes of opened rows whose ranked reason is a re-buy
  (`rebuy_*`) that did not cash: the schedule bought a run the ledger already
  called flat or reverted;
* a DELAYED PAYER is a cashed row that an earlier fill of the same run had
  deferred (same kind, sharing a target unit). `delay_fills` is how many fills
  it waited. Only known after the run cashes, which is why it is a lagging
  reading and the burn streak below is the live one;
* the DRY reading is wall-clock: minutes since the last cashed run CLOSED
  (its `started` plus its `minutes`), or since the run's first row when none
  has cashed. It counts the run still in flight, which the ledger has not
  written yet. The first version counted whole fills with no cash, and on run
  `24951-a72c4642` (2026-09-29) stayed silent for 50 flat minutes because
  fill 1 had cashed +7 at 05:30 before three flat file refills and a per-fn
  run re-hitting one E0277; a fill that cashed once hid its own tail;
* `fill_net` is the net pass delta the newest fill's cashed runs measured.
  The monitor's score moves only at a sweep, so a gain made mid-fill (175 to
  182 on that run) is invisible there until the fill ends.

No test, unit, trait or crate name is an input or a branch.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pipeline_core.domain.fill_schedule import RouteLedgerRow, RunOutcome

__all__ = [
    "MIN_FILL_MINUTES", "DryVerdict", "FillYield", "Payer", "ScheduleYield",
    "dry_verdict", "rows_for", "schedule_yield",
]

MIN_FILL_MINUTES = 5.0
_REBUY_PREFIX = "rebuy_"


@dataclass(frozen=True, slots=True)
class FillYield:
    fill: int
    opened: int
    deferred: int
    spent: float
    cashed: float
    rebuy_minutes: float

    @property
    def yield_share(self) -> float | None:
        return None if self.spent < MIN_FILL_MINUTES else self.cashed / self.spent


@dataclass(frozen=True, slots=True)
class Payer:
    """A run that cashed after the schedule had deferred a sibling of it."""

    fill: int
    label: str
    delay_fills: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScheduleYield:
    fills: tuple[FillYield, ...]
    payers: tuple[Payer, ...]
    cashed_runs: int
    #: When the last cashed run closed; None when nothing cashed.
    last_cash_end: datetime | None = None
    #: When the run's first ledger row started.
    first_start: datetime | None = None
    #: Net pass delta the newest fill's cashed runs measured.
    fill_net: int = 0

    @property
    def spent(self) -> float:
        return sum(f.spent for f in self.fills)

    @property
    def cashed(self) -> float:
        return sum(f.cashed for f in self.fills)

    @property
    def rebuy_minutes(self) -> float:
        return sum(f.rebuy_minutes for f in self.fills)


def _when(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _targets(row: RouteLedgerRow) -> set[tuple[str, str]]:
    """The run's target units. `kept` is what the run wrote, which can be a
    different unit from the one an earlier deferral of the same run named."""
    return {(a, b) for a, b in row.targets}


def schedule_yield(rows: Iterable[RouteLedgerRow]) -> ScheduleYield:
    """Fold one run's rows (any order) into per-fill yield and delayed payers.

    The caller scopes the rows to one run and cluster; mixing runs would pair a
    deferral of one run with the cash of another.
    """
    rows_all = list(rows)
    by_fill: dict[int, list[RouteLedgerRow]] = defaultdict(list)
    for r in rows_all:
        by_fill[r.iteration].append(r)
    fills: list[FillYield] = []
    payers: list[Payer] = []
    cashed_runs = 0
    for it in sorted(by_fill):
        rs = by_fill[it]
        opened = [r for r in rs if r.outcome != RunOutcome.DEFERRED]
        fills.append(FillYield(
            fill=it, opened=len(opened), deferred=len(rs) - len(opened),
            spent=sum(r.minutes or 0.0 for r in opened),
            cashed=sum(r.minutes or 0.0 for r in opened
                       if r.outcome == RunOutcome.CASHED),
            rebuy_minutes=sum(r.minutes or 0.0 for r in opened
                              if r.reason.startswith(_REBUY_PREFIX)
                              and r.outcome != RunOutcome.CASHED)))
        for c in opened:
            if c.outcome != RunOutcome.CASHED:
                continue
            cashed_runs += 1
            keys = _targets(c)
            waited = [d for j in sorted(by_fill) if j < it
                      for d in by_fill[j]
                      if d.outcome == RunOutcome.DEFERRED and d.kind == c.kind
                      and keys & _targets(d)]
            if waited:
                payers.append(Payer(
                    fill=it, label=c.label,
                    delay_fills=len({d.iteration for d in waited}),
                    reasons=tuple(sorted({d.reason for d in waited if d.reason}))))
    ends = [t + timedelta(minutes=r.minutes or 0.0)
            for r in rows_all if r.outcome == RunOutcome.CASHED
            and (t := _when(r.started)) is not None]
    starts = [t for r in rows_all if (t := _when(r.started)) is not None]
    newest = by_fill[max(by_fill)] if by_fill else []
    return ScheduleYield(
        tuple(fills), tuple(payers), cashed_runs,
        last_cash_end=max(ends) if ends else None,
        first_start=min(starts) if starts else None,
        fill_net=sum(r.net_pass_delta for r in newest
                     if r.outcome == RunOutcome.CASHED))


@dataclass(frozen=True, slots=True)
class DryVerdict:
    """The live burn reading, with the threshold it was judged against."""

    burning: bool
    dry_minutes: float
    detail: str


def dry_verdict(y: ScheduleYield, now: datetime, *,
                run_start: datetime | None = None,
                minutes: float = 90.0) -> DryVerdict:
    """Burning = `minutes` or more of wall clock since the last cashed run
    closed (or since the run started, when nothing cashed). `run_start`
    overrides the first ledger row: a run whose first fill has not closed a
    single route run has no row yet and is still spending."""
    since = y.last_cash_end or run_start or y.first_start
    if since is None:
        return DryVerdict(False, 0.0, "no route run recorded")
    m = max(0.0, (now - since).total_seconds() / 60)
    what = "the last cash" if y.last_cash_end else "run start, nothing cashed"
    return DryVerdict(m >= minutes, m, f"{m:.0f} min since {what}")


def rows_for(rows: Sequence[RouteLedgerRow], run_id: str,
             cluster: str | None = None) -> list[RouteLedgerRow]:
    """The rows of one run (and cluster), file order preserved."""
    return [r for r in rows if r.run_id == run_id
            and (cluster is None or r.cluster == cluster)]
