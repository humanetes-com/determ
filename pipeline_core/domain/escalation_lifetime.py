"""The lifetime of a fired futility escalation's product (pure domain).

A fired escalation computes frontier ADDITIONS — producers of the exercised
types plus the traversal steps the futile accessors dispatch through — from
one piece of evidence: "this failing test's attributed unit-set is unchanged
across two judged sweeps". The additions used to be merged into the frontier
only on the firing sweep, guarded by a fire-once set, so on every later sweep
of the same process the frontier reverted to the accessors alone. Run
`2953362-63d4b47e` (2026-08-18, cluster `stable`): iter 1 escalated a 16-unit
accessor frontier to 43 and dispatched 12; iter 2 read a 16-unit frontier
again, so the pass cursor stood at 12 of a cohort whose 27 escalated members
now counted as "left the frontier" — one 4-unit dispatch away from closing
the pass, wiping the cursor and crediting a completed traversal that never
touched the escalated slice. Only the flat-score guard's stop scheduling
pre-empted it.

The rule this module holds: **the product lives exactly as long as the
evidence.** While a fired test still fails and its attribution still equals
the set that earned the fire, the additions stay merged (and therefore stay
in the pass denominator). The moment the evidence changes — the attributed
set moved, an accessor is a stub again (never served by any rewrite, so
"futile" no longer describes it), or the test stopped failing — the product
dies with it, and the test re-earns a fire from the new set under the
ordinary two-sweep rule.

A sustain is NOT new evidence: no guidance suspension, no exhaustion
re-admission, no per-test logging. Those belong to the fire alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline_core.domain.models import FnLocator

#: Why a fired product was dropped this sweep.
DropReason = Literal["evidence_changed", "stub_accessor", "not_failing"]

#: A fired escalation as the loop remembers it: the futile unit-set that
#: EARNED the fire, and the additions the fire computed. The additions are
#: cached rather than recomputed each sweep because the producer scan logs
#: its whole verdict (a recompute would re-print it every sweep), and a
#: process resume recomputes fresh at its own fire anyway.
FiredProduct = tuple[frozenset[FnLocator], dict[FnLocator, set[str]]]


@dataclass(frozen=True, slots=True)
class SustainResult:
    """One sweep's verdict over every fired product."""

    #: Additions to merge into this sweep's frontier, unioned across every
    #: sustained test.
    merges: dict[FnLocator, set[str]]
    #: Tests whose product was re-merged (evidence unchanged since the fire).
    sustained: tuple[str, ...]
    #: Tests whose product died this sweep, with why.
    dropped: tuple[tuple[str, DropReason], ...]


def sustain_escalation_products(
    products: dict[str, FiredProduct],
    units_by_test: dict[str, set[FnLocator]],
    stub_keys: frozenset[FnLocator],
) -> SustainResult:
    """Decide, per fired test, whether its product still has live evidence.

    ``products`` is not mutated — the caller deletes the ``dropped`` entries,
    so the decision stays testable apart from the loop's state.
    """
    merges: dict[FnLocator, set[str]] = {}
    sustained: list[str] = []
    dropped: list[tuple[str, DropReason]] = []
    for tname, (earned, cached) in products.items():
        units = units_by_test.get(tname)
        if units is None:
            dropped.append((tname, "not_failing"))
            continue
        if frozenset(units) != earned:
            dropped.append((tname, "evidence_changed"))
            continue
        if any(u in stub_keys for u in units):
            dropped.append((tname, "stub_accessor"))
            continue
        for key, tests in cached.items():
            merges.setdefault(key, set()).update(tests)
        sustained.append(tname)
    return SustainResult(merges=merges, sustained=tuple(sorted(sustained)),
                         dropped=tuple(sorted(dropped)))
