"""Which runs of one fill would re-ask units an earlier run of the same fill
already asked under evidence nothing has moved since — counted, never
acted on.

WHY THIS EXISTS. `landed_this_fill` withholds a unit from later runs of a
fill after a KEEP. Nothing withholds it after a flat keep, a no-change or a
revert that produced no new evidence, so each route that lists the unit
asks it again. Run `3076-051d385a` (2026-10-13) iter 2: one Debug
implementer was the target of five runs of one fill (a pair 14.3 min,
reverted on 8 newly red; a file run 16.2 min, kept flat; an empty impl run;
a session 37.8 min, no change; a per-fn run 39.3 min, same error set
twice). Only the pair's revert produced new evidence.

WHY IT ONLY COUNTS. Deferring those runs under today's route order (pair
first) would cut the routes the endgame census measures as paying best
(session 2.37 tests/h, file 1.58, against pair 0.21 and per-fn 0.37, since
2026-09-27). Which route asks first is the route-order question, and that
needs its own shadow reading against selection bias before it changes a
run. So this module's verdict is a log line and an event with the minutes
the deferral would have saved; the two turn on together after a reading
shows those runs' rows were flat.

THE RULE. An ask is a unit opened by a run. After a run's row:

* a run that moved a verdict (`newly_green`, a positive net) moves the
  ground for every unit: the set clears;
* a run that reddened tests (`newly_red`) produced new evidence about the
  units it opened: they leave the set, so a later run may ask them with it;
* a run that kept flat, kept nothing, declined or reverted with no newly red
  test asked its opened units and learned nothing: they join the set;
* a deferred, stopped or empty run asked nothing.

A run every target of which is in the set when it opens would be deferred
`asked_this_fill`.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from pipeline_core.domain.fill_schedule import RouteLedgerRow, RunOutcome

__all__ = ["FillAsks"]

_ASKED_NOTHING = frozenset({RunOutcome.DEFERRED, RunOutcome.STOPPED,
                            RunOutcome.EMPTY})


@dataclass
class FillAsks:
    """The units asked this fill under evidence no later run has moved."""

    asked: set[tuple[str, str]] = field(default_factory=set)

    def would_defer(self, targets: Iterable[tuple[str, str]]) -> bool:
        """Every target was already asked under unchanged evidence."""
        ts = set(targets)
        return bool(ts) and ts <= self.asked

    def note(self, row: RouteLedgerRow,
             opened: Iterable[tuple[str, str]]) -> None:
        """Fold one finished run's row into the set (see the module rule)."""
        if row.outcome in _ASKED_NOTHING:
            return
        if row.newly_green or row.net_pass_delta > 0:
            self.asked.clear()
            return
        units = set(opened) or set(row.targets)
        if row.newly_red:
            self.asked -= units
            return
        self.asked |= units
