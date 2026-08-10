"""Progress baselines stamped with the destruction epoch they were measured in.

Several mechanisms destroy progress ON PURPOSE — the crash/layout retrigger
reverts filled bodies to `unimplemented!()`, the batch ratchet restores an
older tree, the merge replaces a cluster's file. Every comparison of the
form `progress_since(baseline)` where the baseline predates such a
destruction is unsatisfiable exactly when the destruction was intentional:
the thing that would satisfy it is the progress the destruction removed.

Run `259637-36db9aad` (2026-08-07) is the measured cost of leaving one
consumer unstamped: the retrigger re-stubbed 56 bodies and invalidated an 81;
three readers were told, the re-admission gate was not, and from iteration 4
every sweep printed `moved pass no further than 81 — NOT re-admitting` while
the only units that could reach 81 sat exhausted behind that very gate.
Nine hours, 148 landed fills, +5 tests.

The fix generalized: a baseline is an :class:`EpochedBaseline` — value plus
the epoch counter at measurement time — and the ONLY comparison offered
refuses to answer across epochs. The caller's obligation on INCOMPARABLE is
to re-base (adopt the current measurement as the new baseline) and try again
next sweep; the helper turns the per-site fix discovered after that deadlock
into the type-enforced default, and retires the "grep every other reader of
the same quantity" obligation by construction. Epoch bumps are the caller's:
one counter per loop, incremented at every deliberate destruction site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProgressComparison(StrEnum):
    """What `current vs baseline` may answer once epochs are in the type."""

    IMPROVED = "improved"
    NOT_IMPROVED = "not_improved"
    INCOMPARABLE = "incomparable"
    """The baseline predates a deliberate destruction — the number describes
    a tree the loop no longer has. Never actionable as `NOT_IMPROVED`: a
    retry gate that reads it that way becomes a permanent lock."""


@dataclass(frozen=True, slots=True)
class EpochedBaseline:
    """A progress measurement plus the destruction epoch it belongs to."""

    value: int
    epoch: int = 0

    def rebased(self, value: int, epoch: int) -> EpochedBaseline:
        """The baseline adopted after an INCOMPARABLE answer: the current
        measurement, stamped with the current epoch."""
        return EpochedBaseline(value=value, epoch=epoch)


def compare_progress(
    current: int, *, baseline: EpochedBaseline, current_epoch: int,
) -> ProgressComparison:
    """The one comparison an epoched baseline supports.

    `current_epoch` is the loop's destruction counter at measurement time,
    not at baseline time — the caller cannot get this wrong in the direction
    that matters, because a stale baseline differs in the epoch FIELD and is
    answered INCOMPARABLE before its value is ever consulted."""
    if baseline.epoch != current_epoch:
        return ProgressComparison.INCOMPARABLE
    if current > baseline.value:
        return ProgressComparison.IMPROVED
    return ProgressComparison.NOT_IMPROVED
