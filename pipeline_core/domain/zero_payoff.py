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

The payoff vector asks what the CRATE did, not what the loop planned.  A sweep
has paid when ANY of: the pass count rose, the crash class moved (the
vanished-test set changed — a crash signature moving IS progress, it re-opens
refuted aims), or the per-binary verdict vector changed (some test, anywhere,
took a different verdict).

The last axis replaces an earlier one — "the frontier changed" — and run
`1064759-0f23fb68` (2026-08-15/16) is why.  That run took seven scored sweeps
of cluster `stable`; six read `pass=79 oracle=101` with BYTE-IDENTICAL
per-binary vectors, so ~70 landed fills changed no test's verdict at all.  This
detector fired 0 times, and not through a wiring fault: it is reached on every
non-grace sweep, and the frontier moved — 60 -> 59 -> 59 -> 30 -> 30 — so every
sweep scored as PAYING on the frontier axis.  A 49% resolution of the plan
against a 0% move of the crate.

The frontier is bookkeeping about the loop's own plan: it shrinks when units
are judged resolved, whether or not resolving them bought anything.  Reading
that as payoff makes the detector structurally unable to fire on a stall where
the loop keeps re-planning — which is every stall of this shape, because
re-planning is what the loop does when nothing works.

The concern the frontier axis existed for is real and is now carried by the
binary vector, which is strictly better at it: score alone cannot distinguish
"filling a producer chain that has not closed" from "locked out", and the
vector answers that directly — a closing chain flips SOME test somewhere long
before the cluster score moves, and a locked-out loop flips none.  The frontier
survives as a tiebreak in exactly one place: the first judged sweep after the
loop's OWN destruction (a retrigger re-stub), where the plan legitimately
changed for a reason the loop caused rather than for a reason it discovered.
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
    pass_delta: int,
    vanished_changed: bool,
    binaries_changed: bool,
    frontier_changed: bool = False,
    *,
    restub_between: bool = False,
) -> bool:
    """Whether a sweep bought anything the CRATE can show.

    `binaries_changed` is the per-binary `(label, passed, failed)` vector of
    the sweep against the previous one: unchanged exactly when no test took a
    different verdict, and immune to the loop's own bookkeeping.

    `frontier_changed` counts only with `restub_between` — a re-stub between
    the two readings means the loop deleted work on purpose and the plan had to
    change; anywhere else, a moving frontier is the loop re-planning and says
    nothing about the tree."""
    return (pass_delta > 0 or vanished_changed or binaries_changed
            or (frontier_changed and restub_between))


def zero_payoff_streak(
    history: tuple[SweepOutcome, ...], window: int = 3,
) -> frozenset[str] | None:
    """The repeated target set, if the last `window` recorded sweeps aimed at
    the IDENTICAL non-empty target set and none paid; else None.

    Identity of the set, not overlap: a shrinking or rotating frontier is the
    loop re-planning, which is payoff on the frontier axis by construction.
    Only the exact same plan, spent K times, with nothing back, qualifies —
    that is the census's futile-aim shape and the spend half of its limit
    cycles."""
    if window < 2 or len(history) < window:
        return None
    tail = history[-window:]
    first = tail[0].targets
    if not first:
        return None
    if all(not o.paid and o.targets == first for o in tail):
        return first
    return None


def unpaid_streak(
    history: tuple[SweepOutcome, ...], window: int = 3,
) -> int | None:
    """How many consecutive recorded sweeps at the tail bought NOTHING, once
    that count reaches `window`; else None.

    The looser sibling of :func:`zero_payoff_streak`, and the one a BATCHED
    loop needs.  Identity of the aim was the right question when a sweep spent
    the whole frontier: repeating the same plan with nothing back is futility
    by construction.  Under a dispatch cap the loop spends a PREFIX of the
    frontier and rotates, so three consecutive sweeps hardly ever aim at the
    identical set even while the crate is provably standing still — run
    `1064759-0f23fb68` (2026-08-15/16) dispatched 12 of 59, then 12 of 59, then
    10 of 30, and its per-binary verdict vector was byte-identical throughout.

    So this rule drops the aim from the CONDITION and keeps it in the REPORT:
    what makes a stall a stall is that nothing was bought, and which units were
    aimed at while nothing was bought is the diagnosis, not the trigger. Both
    rules stay shadow-mode; a fire may hand evidence to a diagnosis step and
    must never revert or block on its own."""
    if window < 2 or len(history) < window:
        return None
    n = 0
    for outcome in reversed(history):
        if outcome.paid:
            break
        n += 1
    return n if n >= window else None
