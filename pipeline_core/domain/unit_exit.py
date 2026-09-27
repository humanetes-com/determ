"""Where did each dispatched unit go? One typed exit per unit per fill.

The dispatch chooses a batch and the routes take what their planners keep. Two
accountings described the gap between them and disagreed: the landed-change
audit's `no route attempted` (the batch minus what the engine reported it
dispatched, minus what landed) and the route ledger's per-run targets. Read
over 80 runs since 2026-08-31 (`scripts/stage_leak_census.py`), 29% of
dispatched units were `unattempted` by the first; joined to the second, 37% of
those sat in an executed run's targets, 46% only in deferred runs' targets and
17% in no run's targets at all — a filter between the dispatch and the route
planners with no recorded reason. Run `38934-2999cc94` (2026-09-26) had the two
units of a recursion in the last bucket on fills 5 and 6.

This module is the one classification. It is pure and changes no decision: the
sets the loop charges strikes and stamps served-at from are untouched; the exit
is what the loop PRINTS and records so a unit that leaves the fill without a
route names where it went, and so an audit can tell a legitimate deferral from
a silent drop by kind instead of by re-deriving the join.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.fill_schedule import RouteLedgerRow, RunOutcome
from pipeline_core.domain.models import FnLocator


class ExitKind(StrEnum):
    LANDED = "landed"
    """A body of the unit moved this fill (an attempt, or a passenger of a
    file write)."""

    ATTEMPTED = "attempted"
    """A route opened the unit and nothing moved (identical reply, revert, a
    declined return)."""

    RUN_TARGET = "run_target"
    """The engine did not report the unit dispatched, yet an EXECUTED run
    opened it (the row's ``opened``), or the rows predate ``opened`` and only
    say the run held it: an answer lost after dispatch, or a route that
    writes a unit its own way without crediting an attempt."""

    FILTERED = "filtered"
    """Executed runs held the unit in their planned targets and none of them
    opened it: the route's own planners dropped it after the schedule fixed
    the targets (the refill collector, the stale probe, a withholding). The
    ledger's ``targets`` alone read this as held, which is why 37% of the
    census's unattempted units looked routed; ``detail`` names the runs."""

    DEFERRED = "deferred"
    """Every run holding the unit was deferred; `detail` lists the reasons
    (`pair_wait`, `premise_moved`, `kind_cap`, …). A recorded reason, not
    necessarily a defect."""

    UNROUTED = "unrouted"
    """No scheduled run held the unit at all: dropped between the dispatch and
    the route planners with no recorded reason. A unit here on consecutive fills
    is the stall shape."""


@dataclass(frozen=True, slots=True)
class UnitExit:
    kind: ExitKind
    detail: str = ""


def unit_exits(
    batch: Iterable[FnLocator],
    *,
    attempted: Collection[FnLocator],
    landed: Collection[FnLocator],
    rows: Sequence[RouteLedgerRow],
) -> dict[FnLocator, UnitExit]:
    """The exit of every unit of ``batch``.

    ``rows`` are the ledger rows of THIS fill only (the caller filters run,
    cluster and iteration). The classification of the units the audit calls
    unattempted (`batch - attempted - landed`) is the join the census computes:
    an executed run that opened it (or whose row predates ``opened``), then
    executed runs that held it and opened none of it, then deferred runs'
    targets, else none."""
    held: dict[FnLocator, list[RouteLedgerRow]] = {}
    for r in rows:
        for f, n in r.targets:
            held.setdefault(FnLocator(f, n), []).append(r)
    out: dict[FnLocator, UnitExit] = {}
    for u in batch:
        if u in landed:
            out[u] = UnitExit(ExitKind.LANDED)
        elif u in attempted:
            out[u] = UnitExit(ExitKind.ATTEMPTED)
        else:
            runs = held.get(u, [])
            executed = [r for r in runs if r.outcome is not RunOutcome.DEFERRED]
            key = (u.file, u.fn_name)
            if executed and all(r.opened is not None and key not in r.opened
                                for r in executed):
                out[u] = UnitExit(ExitKind.FILTERED,
                                  ", ".join(sorted({r.label for r in executed})))
            elif executed:
                out[u] = UnitExit(ExitKind.RUN_TARGET,
                                  ", ".join(sorted({r.label for r in executed})))
            elif runs:
                out[u] = UnitExit(ExitKind.DEFERRED, ", ".join(
                    sorted({r.reason or "deferred" for r in runs})))
            else:
                out[u] = UnitExit(ExitKind.UNROUTED)
    return out


def exit_histogram(exits: dict[FnLocator, UnitExit]) -> Counter[ExitKind]:
    return Counter(e.kind for e in exits.values())


def exit_line(exits: dict[FnLocator, UnitExit]) -> str:
    """The log suffix: the histogram, the deferral reasons and the units that
    left the fill with no route (named, up to eight)."""
    hist = exit_histogram(exits)
    parts = [f"{k.value} {hist[k]}" for k in ExitKind if hist[k]]
    why = Counter(r.strip() for e in exits.values()
                  if e.kind is ExitKind.DEFERRED for r in e.detail.split(","))
    text = "exits: " + ", ".join(parts)
    if why:
        text += " (deferred by " + ", ".join(
            f"{k} {v}" for k, v in sorted(why.items())) + ")"
    dropped = sorted(f"{k.file}::{k.fn_name}" for k, e in exits.items()
                     if e.kind is ExitKind.FILTERED)
    if dropped:
        text += "; filtered inside a run: " + ", ".join(dropped[:8])
    lost = sorted(f"{k.file}::{k.fn_name}" for k, e in exits.items()
                  if e.kind is ExitKind.UNROUTED)
    if lost:
        text += "; held by NO run: " + ", ".join(lost[:8])
    return text


def exits_payload(exits: dict[FnLocator, UnitExit]) -> dict[str, object]:
    """The event payload: the histogram and, for every unit that did not land
    or get attempted, where it went."""
    return {
        "histogram": {k.value: v for k, v in exit_histogram(exits).items()},
        "left": {f"{k.file}::{k.fn_name}": [e.kind.value, e.detail]
                 for k, e in sorted(exits.items())
                 if e.kind not in (ExitKind.LANDED, ExitKind.ATTEMPTED)},
    }
