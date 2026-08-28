"""Whether a fill pass could have moved any verdict at all (pure domain).

A pass ends by asking "did anything fill?" — `progress_this_pass == 0` stops it.
That question cannot see the case measured on run `2879-66905005`:
71 units, 40 of them landing bodies, ~2.7 h and the bulk of 3.3 M prompt tokens
spent, and **not one of those bodies could be judged**, because every test
attributed to them panicked inside the same still-stubbed sibling
(`error.rs::construct`). Progress was 40; payoff was 0.

The pipeline already knew. Each such unit is kept via
``U2Decision.ACCEPT_EXTERNAL_ONLY`` and says so — "N attributed test(s) blocked
by still-stubbed sibling(s) — deferred to outer loop", `phase_c.u2.external_only`
with a `blocking_fns` list. Fifty of those events went out in that run. Nothing
read them together, so the pass proceeded to spend its budget on units that
could not score no matter what the model returned.

**The rule is an intersection, not an equality.** "Every unit named the same
blockers" is too strict — units legitimately differ in which sibling holds their
own tests. What makes a pass provably payoff-zero is that *some* fn blocks
**all** of them: until it fills, no unit in the pass can be judged, so no
verdict can move whatever any body says. The intersection is also what makes the
finding actionable — it names the fn to escalate to, rather than reporting that
a pass was wasted.

A pass can still be wholly unjudgeable with an empty intersection: every landed
body blocked, each by its own producer. Run `155810-cbb3b9fc` iter 1 (2026-08-28)
landed 23 bodies, all `keep_deferred`, and printed no PAYOFF ZERO line because
the 23 units spanned 7 files. `is_zero` stays the proof. `unjudgeable_rate` and
`blocker_counts` are the rate and the histogram that do not need a universal
blocker — the caller acts on rate == 1.0 and ranks the next dispatch by the
counts.

Refusing costs nothing when the reading is wrong: the units keep their bodies
(they compiled), and the outer TDD loop re-runs them once the blocker fills.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline_core.domain.models import FnLocator, FnSite

if TYPE_CHECKING:
    from pipeline_core.domain.models import FillResult


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    """One fill unit's contribution to the pass's payoff question.

    The routes disagree about what a unit IS — the per-fn route fills one fn,
    the impl route one whole block — and they carry different result types.
    They agree on the only two facts this rule needs: did a body land, and if
    so, which still-stubbed sites held every test attributed to it."""

    landed: bool
    blocked_by: tuple[FnSite, ...] = ()

    @classmethod
    def of_fill(cls, res: FillResult) -> UnitOutcome:
        return cls(landed=res.filled, blocked_by=res.blocked_by)


@dataclass(frozen=True, slots=True)
class PassPayoff:
    """What a completed pass could have moved, and what stood in the way.

    `blockers` is non-empty only for the provable case: every unit that landed a
    body was kept external-only, and these sites blocked all of them.

    `landed_blocked` / `unjudgeable_rate` / `blocker_counts` do not require a
    universal blocker. They describe the case `is_zero` cannot name: every
    landed body unjudgeable, each held by a different producer.
    """

    blockers: frozenset[FnSite]
    landed: int
    landed_blocked: int = 0
    blocker_counts: tuple[tuple[FnSite, int], ...] = ()

    @property
    def is_zero(self) -> bool:
        """True iff this pass provably could not have moved a verdict.

        Requires bodies to have landed — a pass that filled nothing is the
        ordinary no-progress case, which the loop already stops on, and calling
        it payoff-zero would blame a blocker for an empty pass."""
        return self.landed > 0 and bool(self.blockers)

    @property
    def unjudgeable_rate(self) -> float:
        """Fraction of landed bodies that could not be judged on their own
        tests. 0.0 when nothing landed — an empty pass is not a rate."""
        if self.landed <= 0:
            return 0.0
        return self.landed_blocked / self.landed

    @property
    def summary(self) -> str:
        names = ", ".join(sorted(f"{s.file}::{s.fn_name}" for s in self.blockers))
        return (f"{self.landed} body/bodies landed, none judgeable — "
                f"blocked for every unit by: {names}")

    @property
    def rate_summary(self) -> str:
        """The rate line: printed when every landed body was unjudgeable,
        including the empty-intersection case `summary` cannot name."""
        hist = ", ".join(
            f"{s.file}::{s.fn_name} ({n})" for s, n in self.blocker_counts[:8])
        head = (f"{self.landed_blocked}/{self.landed} landed bodies "
                "unjudgeable")
        if self.blockers:
            return f"{head} — {self.summary}"
        if hist:
            return f"{head} — no universal blocker; top holders: {hist}"
        return f"{head} — no universal blocker"

    def locator_counts(self) -> dict[FnLocator, int]:
        """`blocker_counts` collapsed to the dispatch key (file, fn)."""
        out: dict[FnLocator, int] = {}
        for site, n in self.blocker_counts:
            key = FnLocator(site.file, site.fn_name)
            out[key] = out.get(key, 0) + n
        return out

    def event_data(self) -> dict[str, object]:
        """Payload for `phase_c.pass.payoff_zero` / the unjudgeable sibling."""
        return {
            "landed": self.landed,
            "landed_blocked": self.landed_blocked,
            "unjudgeable_rate": self.unjudgeable_rate,
            "blocking_fns": sorted(
                f"{s.file}::{s.fn_name}" for s in self.blockers),
            "blocker_counts": [
                [f"{s.file}::{s.fn_name}", n] for s, n in self.blocker_counts
            ],
        }


def assess_pass_payoff(outcomes: Iterable[UnitOutcome]) -> PassPayoff:
    """The pass's payoff, from the units' own verdicts.

    A unit that landed a body and was judged on its own merits (`blocked_by`
    empty) is proof the pass was not payoff-zero — whatever else happened, at
    least one verdict was reachable. One such unit clears `blockers`. The
    rate and histogram still count every landed body: a mixed pass is not
    `is_zero` and is also not silent about how many units paid nothing."""
    landed = 0
    landed_blocked = 0
    common: frozenset[FnSite] | None = None
    counts: Counter[FnSite] = Counter()
    judged = False
    for out in outcomes:
        if not out.landed:
            continue
        landed += 1
        if not out.blocked_by:
            judged = True
            continue
        landed_blocked += 1
        for site in out.blocked_by:
            counts[site] += 1
        sites = frozenset(out.blocked_by)
        common = sites if common is None else (common & sites)
    blockers: frozenset[FnSite] = frozenset()
    if landed and not judged and common:
        blockers = common
    ranked = tuple(sorted(counts.items(), key=lambda sn: (-sn[1],
                         sn[0].file, sn[0].fn_name, sn[0].container)))
    return PassPayoff(blockers, landed=landed, landed_blocked=landed_blocked,
                      blocker_counts=ranked)


def assess_fill_results(results: Iterable[FillResult]) -> PassPayoff:
    """`assess_pass_payoff` over the per-fn route's result type."""
    return assess_pass_payoff(UnitOutcome.of_fill(r) for r in results)


def merge_locator_counts(
    *sources: dict[FnLocator, int] | None,
) -> dict[FnLocator, int]:
    """Sum blocker histograms. Empty/None sources drop out; an all-empty
    result is the signal that the caller should fall back to the pre-dispatch
    deferral map."""
    out: dict[FnLocator, int] = {}
    for src in sources:
        if not src:
            continue
        for key, n in src.items():
            out[key] = out.get(key, 0) + n
    return out


def combine_payoffs(*payoffs: PassPayoff | None) -> PassPayoff | None:
    """Merge impl-route and per-fn payoffs from one engine call. Histograms
    add; `is_zero` stays a proof — only if every part was `is_zero` does the
    combined intersection survive."""
    present = [p for p in payoffs if p is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    landed = sum(p.landed for p in present)
    landed_blocked = sum(p.landed_blocked for p in present)
    counts: Counter[FnSite] = Counter()
    for p in present:
        for site, n in p.blocker_counts:
            counts[site] += n
    blockers: frozenset[FnSite] = frozenset()
    if present and all(p.is_zero for p in present):
        blockers = present[0].blockers
        for p in present[1:]:
            blockers &= p.blockers
    ranked = tuple(sorted(counts.items(), key=lambda sn: (-sn[1],
                         sn[0].file, sn[0].fn_name, sn[0].container)))
    return PassPayoff(blockers, landed=landed, landed_blocked=landed_blocked,
                      blocker_counts=ranked)
