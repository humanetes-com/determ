"""One iteration's slice of the TDD frontier, and the pass that slice belongs to.

WHY THIS EXISTS. The TDD sweep is the loop's only measurement of the tree, and
it is nearly free: the cargo runs on the iteration lines cost `rc=101 (0.4s)`,
`(1.0s)`, `(1.1s)`. What separates two sweeps is the fill batch between them,
and that batch was the WHOLE frontier. Run `669347-cf33eae3` (2026-08-15)
dispatched 55 impl blocks plus 9 file units in one batch and logged exactly one
`phase_c.tdd.iter` in 4h17m. Measured from `phase_c.tdd.iter` timestamps in
`logs/events.jsonl`, one iteration costs 198-253 minutes:

    3368-753757e6    4 iters   gaps 0, 198, 253 min   (life 458m05s)
    263217-d41a5bb5  2 iters   gap 243 min            (life 308m29s)
    500301-6a66bf86  1 iter    -                      (life 214m10s)

against run lifetimes of 214-458 minutes. So a run yields one or two data
points, and every gate that acts at a sweep boundary — the batch ratchet,
re-admission, the crash retrigger, the plateau exit — gets one or two chances
to fire in a whole run. Slicing the frontier makes the loop LOOK more often at
the same model cost. It does not change WHAT gets filled or in what order:
`order_frontier` still ranks the entire frontier and the batch takes the next
not-yet-attempted units off that ranking.

THE PASS IS THE UNIT THE STALL BUDGET IS DENOMINATED IN. `TDD_MAX_STALLS` asks
"have the last N iterations of fills failed to move the score". With a capped
batch an iteration attempts only part of the frontier, so charging the stall on
every iteration would make the plateau exit fire (frontier/cap) times sooner in
wall-clock than it does today — trading a sampling defect for a premature-exit
one. A PASS is one traversal of the frontier; only the sweep that follows a
COMPLETED pass may charge the stall, which restores the pre-cap meaning exactly.

THE CURSOR IS A SET, NOT AN INDEX. The frontier is rebuilt from the failing
tests every iteration and shrinks as fills land, so a positional cursor over a
changing list would keep re-dispatching the head and starve the tail — the same
shape as the "iteration spent on the blocked instead of the blockers" pathology
the loop already carries scar tissue for. Tracking which units this pass has
already attempted is stable under that churn: units that leave the frontier
simply never come back for their turn, and units that arrive mid-pass are
picked up before the pass closes.
"""
from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass

#: Frontier units dispatched per iteration when the cap is left at its default.
#: 12 against the live frontier of 55-69 puts a sweep roughly every 45 minutes
#: at the ~4h/iteration rate measured above, i.e. five to six measurements where
#: the loop previously took one.
DEFAULT_MAX_DISPATCH = 12


@dataclass(frozen=True, slots=True)
class DispatchBatch[K: Hashable]:
    """The units one iteration will dispatch, and whether taking them closes a
    pass over the frontier."""

    keys: tuple[K, ...]
    #: True when this batch exhausts what the current pass had left, so the
    #: NEXT sweep measures a completed traversal and may charge the stall.
    completes_pass: bool
    #: How many of the frontier's units this pass had already attempted before
    #: this batch. 0 on the first batch of a pass.
    attempted_before: int
    #: The whole frontier this batch was drawn from.
    frontier_size: int

    @property
    def capped(self) -> bool:
        """True when the cap actually withheld work — i.e. the batch is a
        strict subset of what the pass had pending. False when the batch took
        everything left, which is the uncapped loop's behaviour."""
        return not self.completes_pass

    @property
    def rendered(self) -> str:
        """`taken/frontier` for the iteration line, with a pass marker so a
        reader can tell a mid-pass sweep from one that closed a traversal."""
        done = self.attempted_before + len(self.keys)
        return (f"{len(self.keys)} of {self.frontier_size} "
                f"({done}/{self.frontier_size} this pass"
                f"{'' if self.completes_pass else ', mid-pass'})")


def take_batch[K: Hashable](ordered: Sequence[K], attempted: Iterable[K],
                            cap: int) -> DispatchBatch[K]:
    """Next slice of `ordered` this pass has not attempted yet.

    `ordered` is the frontier in dispatch rank (leaf-first), `attempted` is what
    the current pass has already handed to the engine. A `cap` of 0 or less
    disables batching and returns the whole pending set, which is the pre-cap
    behaviour — keep that available, because a small crate whose frontier is
    already under the cap should not pay for the machinery.

    When the pass has nothing pending (every frontier unit already attempted, or
    the frontier shrank below what was attempted) the pass has closed: a fresh
    one starts here, over the frontier as it stands NOW rather than as it stood
    when the last pass opened.
    """
    seen = set(attempted)
    pending = [k for k in ordered if k not in seen]
    if not pending:
        # Pass closed. Reopen on the current frontier; `attempted_before` is 0
        # because none of THIS pass's traversal has happened yet.
        pending = list(ordered)
        seen = set()
    attempted_before = len(ordered) - len(pending)
    if cap <= 0 or len(pending) <= cap:
        return DispatchBatch(keys=tuple(pending), completes_pass=True,
                             attempted_before=attempted_before,
                             frontier_size=len(ordered))
    return DispatchBatch(keys=tuple(pending[:cap]), completes_pass=False,
                         attempted_before=attempted_before,
                         frontier_size=len(ordered))
