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
evidence.** Evidence is "this failing test still fails" (and no attributed
accessor is a stub again). The product is keyed by TEST, not by the
accessor set that earned the fire: an attribution shift that still names
the same failing test is not new evidence — it is the re-buy that
persisting the product exists to close (run `4911`/`2932`, 2026-08-25/26:
the trigger seed keyed on exact accessor-set equality and iter 1
re-dispatched the new accessors). A stubbed accessor or a passing test
still kills the product, and the test re-earns a fire from the live set
under the ordinary two-sweep rule.

A sustain is NOT new evidence: no guidance suspension, no exhaustion
re-admission, no per-test logging. Those belong to the fire alone.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pipeline_core.domain.models import FnLocator

#: Why a fired product was dropped this sweep.
DropReason = Literal["stub_accessor", "not_failing"]

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
    for tname, (_earned, cached) in products.items():
        units = units_by_test.get(tname)
        if units is None:
            dropped.append((tname, "not_failing"))
            continue
        if any(u in stub_keys for u in (units or ())):
            dropped.append((tname, "stub_accessor"))
            continue
        for key, tests in cached.items():
            merges.setdefault(key, set()).update(tests)
        sustained.append(tname)
    return SustainResult(merges=merges, sustained=tuple(sorted(sustained)),
                         dropped=tuple(sorted(dropped)))


def product_addition_keys(
    products: Mapping[str, FiredProduct],
) -> frozenset[FnLocator]:
    """Units the fired products cache as frontier additions.

    This is the set `promote_product_ahead` ranks, not `escalation_producers`.
    The producers dict is a prompt-grade split (constructors vs traversal
    steps) filled through `producers_out`; the harness stubs that return the
    extras dict without writing that side channel would otherwise leave
    ranking empty, and persist would save an empty producer tuple. The
    cached additions are what sustain actually merges, and they survive
    resume even when the side channel was empty (the cap=1 attribution
    shift: new accessor at slot 0, producer parked).
    """
    return frozenset(
        k for _earned, cached in products.values() for k in cached)


@dataclass(frozen=True, slots=True)
class FireDecision:
    """Whether a not-yet-fired test earns its escalation this sweep."""

    due: bool
    #: Attributed units the evidence does NOT cover: never dispatched since
    #: this unit-set was first observed. Non-empty only when the set is
    #: unchanged (the old rule would have fired) and service is what is
    #: missing — the case worth a log line.
    unserved: tuple[FnLocator, ...]


def escalation_fire_due(
    units: frozenset[FnLocator],
    prev: frozenset[FnLocator] | None,
    served_at: Mapping[FnLocator, int],
    since: int,
) -> FireDecision:
    """The fire rule, with the premise it always had made explicit.

    "This failing test's attributed unit-set is unchanged across two judged
    sweeps" was the whole trigger, and it MEANT "its accessors were served by
    rewrites that changed nothing" — true by construction while every
    frontier unit was dispatched every sweep. Under the dispatch cap
    (`PHASE_C_TDD_MAX_DISPATCH`, 12 of a 28-unit frontier) identity no longer
    implies service: a unit parked in batch 3 has an identical set at iters 1
    and 2 because NOTHING happened to it. Run `263474-b0e608d3`, iter 2
    (2026-08-25): `test_altdebug`'s owner `src/fmt.rs::debug` — attributed at
    iter 1, never dispatched — was declared futile on identity alone and the
    test escalated to `construct`/`erase`, the (correct) producers of
    `ErrorImpl`; the accessor then meets its batch carrying a "measured
    futile" grade that steers the rewrite away from the body the verdict
    pins. That is the mis-target class, manufactured by the gate.

    So the rule is: unchanged set AND every unit in it dispatched at or after
    the sweep the set was first observed (`served_at[u] >= since`). With the
    cap off every unit is served every sweep and this is byte-for-byte the
    old rule; a persisted futility seed (the floor's `futile_units`, resumed
    across a code move) counts as served at 0, since that evidence IS the
    record of service — the resumed process still escalates at its first
    sweep."""
    if prev is None or prev != units:
        return FireDecision(due=False, unserved=())
    unserved = tuple(sorted(
        (u for u in units if served_at.get(u, -1) < since),
        key=lambda u: (u.file, u.fn_name)))
    return FireDecision(due=not unserved, unserved=unserved)
