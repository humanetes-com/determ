"""The generic zero-payoff detector — SHADOW MODE.

One condition underlies every stall class the 2026-08-14 pattern census
(`scripts/pattern_census.py`, 34 anyhow runs) measured: the loop repeats an
action whose expected information gain is zero.  The bespoke detectors each
recognize one instance — the crash-signature memory sees a re-fired component,
the sibling-blocked check sees a blocked U2 batch, the stall budget sees a flat
score — and each took days to ship after its class first fired (frame-blind
NOBODY blame: first hit 2026-08-04 run `525803-9a51be5a`, first reaction
2026-08-13).  This module is the class-general form: over a window of K
sweeps, the same non-empty target set was aimed at and every sweep paid
nothing back.

SHADOW MODE, deliberately.  The detector LOGS and takes no action, gates
nothing, and no mechanism may read it yet.  It earns authority the way a walk
does: its firing lines on live runs are compared against the runs' actual
outcomes until the window and the payoff vector are calibrated — a detector
that fires on a healthy producer-chain build (score flat while the chain has
not closed) would convert patience into panic, which is why `improved`-style
counters alone were never enough.  Wire-up rule for the future non-shadow
form: a fire may only ever hand evidence to a diagnosis step, never revert or
block on its own.

The payoff vector is three-axis on purpose.  A sweep has paid when ANY of:
pass count rose, the crash class moved (the vanished-test set changed — a
crash signature moving IS progress, it re-opens refuted aims), or the frontier
changed (new fns became reachable).  Score alone cannot distinguish "filling
a producer chain that has not closed" from "locked out" — the frontier axis
is what separates them, mirroring how the census's healthy negative controls
(26 of 39 qualifying runs when measured 2026-08-14; the totals grow with the
log rotation) stay clean.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one sweep aimed at and what the NEXT sweep showed it bought.

    `targets` is the frontier the iteration dispatched (the spend);
    `paid` is whether any payoff axis moved by the time of the next reading.
    Sweeps inside a re-fill grace window must not be recorded at all: their
    deltas measure a deliberate destruction, not the spend
    (the same exemption every score consumer already applies)."""
    targets: frozenset[str]
    paid: bool


def paid_off(
    pass_delta: int, vanished_changed: bool, frontier_changed: bool,
) -> bool:
    """Whether a sweep bought anything on any axis."""
    return pass_delta > 0 or vanished_changed or frontier_changed


def zero_payoff_streak(
    history: tuple[SweepOutcome, ...], window: int = 3,
) -> frozenset[str] | None:
    """The repeated target set, if the last `window` recorded sweeps aimed at
    the IDENTICAL non-empty target set and none paid; else None.

    Identity of the set, not overlap: a shrinking or rotating frontier is the
    loop re-planning, which is payoff on the frontier axis by construction.
    Only the exact same plan, spent K times, with nothing back, qualifies —
    that is the census's futile-aim shape (P3) and the spend half of its limit
    cycles (P5)."""
    if window < 2 or len(history) < window:
        return None
    tail = history[-window:]
    first = tail[0].targets
    if not first:
        return None
    if all(not o.paid and o.targets == first for o in tail):
        return first
    return None
