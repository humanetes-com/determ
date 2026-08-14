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

from pipeline_core.domain.refine import readmission_warranted


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


class BoundaryDecision(StrEnum):
    """What one iteration boundary decided about the re-admission valve."""

    RELEASE = "release"            # fills since the last reset moved the score
    REFUSE = "refuse"              # fills landed, score did not pass the baseline
    DEFER_TAINTED = "defer_tainted"      # this sweep's score disclaimed itself
    DEFER_UNMEASURED = "defer_unmeasured"  # INCOMPARABLE, and no measurement
    #                                        exists to re-base onto
    NONE = "none"                  # nothing to decide (no fills landed)


@dataclass(frozen=True, slots=True)
class BoundaryOutcome:
    """`readmission_boundary`'s verdict plus the baseline the loop must carry
    forward. `rebased_from` is the value an epoch re-base replaced (None when
    no value moved) — it exists so the caller can log the re-base as the
    signal it is without re-deriving the comparison."""

    decision: BoundaryDecision
    baseline: EpochedBaseline
    rebased_from: int | None = None


def readmission_boundary(
    *, best_pass: int, baseline: EpochedBaseline, destruction_epoch: int,
    new_fills: int, sweep_measured: bool, sweep_reconciled: bool,
) -> BoundaryOutcome:
    """The whole re-admission boundary as one ordered, pure decision.

    ORDER IS THE CONTRACT, and it is the reason this function exists. The
    epoch re-base adopts `best_pass` as the new baseline, so it is only
    correct AFTER `best_pass` has been made to describe the tree on disk —
    i.e. after the sweep and after the high-water's own destruction re-base.
    Run `58819-ee78a1d1` (2026-08-13) executed these same steps with the
    re-base at the PRE-sweep boundary: the iter-6 epoch re-base adopted 79
    while the tree measured 74 (the retrigger fired after iter 5's sweep, so
    nothing had re-based the watermark yet), the log printed `re-admission
    baseline 74 predates a deliberate destruction — re-based to 79` followed
    by `high-water re-based 79 -> 74`, and the release that iter 5's genuine
    74 -> 79 re-climb had earned was consumed by bookkeeping: 79 > 79 never
    fires — the permanent-lock class this module's docstring opens with,
    recurring INSIDE the mechanism built to prevent it, via consumer
    ordering. The caller's obligation is to invoke this with the POST-sweep,
    post-high-water-re-base `best_pass`.

    `sweep_measured` is False for a compile-broken sweep: it measured
    nothing, so it can neither re-base a destruction-invalidated baseline nor
    justify a decision against it — the boundary defers whole, and the next
    measured sweep resolves the INCOMPARABLE instead.

    An unspent free grant (negative sentinel baseline) keeps its value
    through an epoch re-base — it is not a baseline yet, and re-basing it
    would spend it on bookkeeping."""
    cmp = compare_progress(
        best_pass, baseline=baseline, current_epoch=destruction_epoch)
    rebased_from: int | None = None
    if cmp is ProgressComparison.INCOMPARABLE:
        if not sweep_measured:
            return BoundaryOutcome(BoundaryDecision.DEFER_UNMEASURED, baseline)
        if baseline.value >= 0 and baseline.value != best_pass:
            rebased_from = baseline.value
        baseline = baseline.rebased(
            best_pass if baseline.value >= 0 else baseline.value,
            destruction_epoch)
    if not sweep_reconciled and new_fills > 0:
        return BoundaryOutcome(
            BoundaryDecision.DEFER_TAINTED, baseline, rebased_from)
    if readmission_warranted(
            fills_since_reset=new_fills, best_metric=best_pass,
            metric_at_last_reset=baseline.value):
        return BoundaryOutcome(
            BoundaryDecision.RELEASE,
            baseline.rebased(best_pass, destruction_epoch), rebased_from)
    if new_fills > 0:
        return BoundaryOutcome(
            BoundaryDecision.REFUSE, baseline, rebased_from)
    return BoundaryOutcome(BoundaryDecision.NONE, baseline, rebased_from)
