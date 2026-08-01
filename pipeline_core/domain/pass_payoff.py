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

Refusing costs nothing when the reading is wrong: the units keep their bodies
(they compiled), and the outer TDD loop re-runs them once the blocker fills.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pipeline_core.domain.models import FillResult, FnSite


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
    """

    blockers: frozenset[FnSite]
    landed: int

    @property
    def is_zero(self) -> bool:
        """True iff this pass provably could not have moved a verdict.

        Requires bodies to have landed — a pass that filled nothing is the
        ordinary no-progress case, which the loop already stops on, and calling
        it payoff-zero would blame a blocker for an empty pass."""
        return self.landed > 0 and bool(self.blockers)

    @property
    def summary(self) -> str:
        names = ", ".join(sorted(f"{s.file}::{s.fn_name}" for s in self.blockers))
        return (f"{self.landed} body/bodies landed, none judgeable — "
                f"blocked for every unit by: {names}")


def assess_pass_payoff(outcomes: Iterable[UnitOutcome]) -> PassPayoff:
    """The pass's payoff, from the units' own verdicts.

    A unit that landed a body and was judged on its own merits (`blocked_by`
    empty) is proof the pass was not payoff-zero — whatever else happened, at
    least one verdict was reachable. One such unit ends the assessment."""
    landed = 0
    common: frozenset[FnSite] | None = None
    for out in outcomes:
        if not out.landed:
            continue
        landed += 1
        if not out.blocked_by:
            return PassPayoff(frozenset(), landed=landed)
        sites = frozenset(out.blocked_by)
        common = sites if common is None else (common & sites)
    if not landed or not common:
        return PassPayoff(frozenset(), landed=landed)
    return PassPayoff(common, landed=landed)


def assess_fill_results(results: Iterable[FillResult]) -> PassPayoff:
    """`assess_pass_payoff` over the per-fn route's result type."""
    return assess_pass_payoff(UnitOutcome.of_fill(r) for r in results)
