"""Which runs of one fill would re-ask units an earlier run of the same fill
already asked under evidence nothing has moved since.

WHY THIS EXISTS. `landed_this_fill` withholds a unit from later runs of a
fill after a KEEP. Nothing withholds it after a flat keep, a no-change or a
revert that produced no new evidence, so each route that lists the unit
asks it again. Run `3076-051d385a` (2026-10-13) iter 2: one Debug
implementer was the target of five runs of one fill (a pair 14.3 min,
reverted on 8 newly red; a file run 16.2 min, kept flat; an empty impl run;
a session 37.8 min, no change; a per-fn run 39.3 min, same error set
twice). Only the pair's revert produced new evidence.

WHAT IT DEFERS. The shadow reading this module waited on was taken
2026-10-17 over every shadowed run since 2026-10-13: 15 runs, all impl or
per-fn, 175 minutes, 5 flat, 3 reverted, 6 empty, none paid a test. The
executor now defers a non-session run every target of which is in the set
(`asked_this_fill`). A session's question is its slice and its turns, not
its units, so a session is never deferred here and stays a shadow line;
the route-order concern this module once answered (a session deferred
behind an earlier pair) does not arise.

THE RULE. An ask is a unit opened by a run. After a run's row:

* a run that moved a verdict (`newly_green`, a positive net) moves the
  ground for every unit: the set clears;
* a run that reddened tests (`newly_red`) produced new evidence about the
  units it opened: they leave the set, so a later run may ask them with it;
* a run reverted on a compile failure judged the joint write, not each unit:
  they leave the set, so a narrower route may land them;
* a run that kept flat, kept nothing, declined or reverted with no newly red
  test asked its opened units and learned nothing: they join the set;
* a deferred, stopped or empty run asked nothing, and neither did one whose
  reply carried no body (an answer dropped).

A run every target of which is in the set when it opens would be deferred
`asked_this_fill`.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from pipeline_core.domain.fill_schedule import RouteLedgerRow, RunOutcome

__all__ = ["FillAsks"]

_ASKED_NOTHING = frozenset({RunOutcome.DEFERRED, RunOutcome.STOPPED,
                            *(o for o in RunOutcome if o.opened_nothing)})
# A revert whose classes include a compile failure judged the JOINT write:
# a narrower write (one block, one fn) can land what the joint could not, so
# its units are not "asked under unchanged evidence". The per-block fallback
# after a file route's compile revert is that narrower ask.
_COMPILE = "compile"
_DROPPED = "dropped"


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
        if row.classes.get(_DROPPED) and not row.kept and not row.reverted:
            # The reply carried no body to judge (an answer dropped): no
            # question about these units was answered, the same rule a
            # session with no body follows (`session_row_tried`).
            return
        if row.newly_green or row.net_pass_delta > 0:
            self.asked.clear()
            return
        units = set(opened) or set(row.targets)
        if row.newly_red or (row.outcome is RunOutcome.REVERTED
                             and row.classes.get(_COMPILE)):
            self.asked -= units
            return
        self.asked |= units
