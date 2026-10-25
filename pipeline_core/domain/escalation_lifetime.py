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

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from pipeline_core.domain.fill_schedule import DEFAULT_UNPAID_GRACE
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.oracle_ledger import bare_test_name

#: Why a fired product was dropped this sweep.
DropReason = Literal["stub_accessor", "not_failing", "format_trait_test"]

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
    format_tests: frozenset[str] = frozenset(),
) -> SustainResult:
    """Decide, per fired test, whether its product still has live evidence.

    ``products`` is not mutated — the caller deletes the ``dropped`` entries,
    so the decision stays testable apart from the loop's state.

    ``format_tests`` are fired tests whose own source renders through a
    format trait (`{:?}` / `{}` / `.to_string()`). Their products are dropped:
    a producer escalation answers "who builds the accessors' container", and
    no builder changes what the format trait emits — the format-implementer
    attribution owns that evidence. The gate lives at sustain as well as at
    fire because products are floor-persisted: a product fired (or resumed)
    before the fire-side gate existed would otherwise re-merge every sweep
    (the 42-unit constructor family of run `114436-9cb17382`).
    """
    merges: dict[FnLocator, set[str]] = {}
    sustained: list[str] = []
    dropped: list[tuple[str, DropReason]] = []
    for tname, (_earned, cached) in products.items():
        units = units_by_test.get(tname)
        if units is None:
            dropped.append((tname, "not_failing"))
            continue
        if tname in format_tests:
            dropped.append((tname, "format_trait_test"))
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

    This is the set the rank's `escalated` fact reads, not `escalation_producers`.
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
    unexecuted: Collection[FnLocator] = (),
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
    sweep.

    ``unexecuted`` is the units the last measured sweep proved no failing
    test executes (`coverage_slice.red_dead_units`, positive evidence
    only). Such a unit is never dispatched — it ranks last — and no rewrite
    of it can move the test, so waiting for its service is waiting for
    nothing. Run `486447-5207101c` (2026-10-07): ten `ensure!` tests were
    held at `NOT escalating — … never dispatched: src/lib.rs::Ok` (an
    identifier-scan attribution the coverage reading contradicts) on the
    sweep whose service order classed `Ok` red-dead."""
    if prev is None or prev != units:
        return FireDecision(due=False, unserved=())
    skip = frozenset(unexecuted)
    unserved = tuple(sorted(
        (u for u in units if u not in skip and served_at.get(u, -1) < since),
        key=lambda u: (u.file, u.fn_name)))
    return FireDecision(due=not unserved, unserved=unserved)


def red_dead_after_sweep(
    previous: frozenset[FnLocator],
    measured: frozenset[FnLocator] | None,
) -> frozenset[FnLocator]:
    """The red-dead set the next fire reads (`escalation_fire_due`'s
    ``unexecuted``): this sweep's when it measured coverage, else the last
    measured one. A sweep with no coverage reading is not evidence that a
    unit became executed; replacing the set with empty there re-armed the
    hold on `src/lib.rs::Ok` the sweep after. ``measured`` None is the
    miss."""
    return previous if measured is None else measured


def measured_owners_owed(
    admitted: Collection[FnLocator], ordered: Iterable[FnLocator], *,
    batch: Collection[FnLocator], starved: Collection[FnLocator],
) -> frozenset[FnLocator]:
    """The measured coverage owners the schedule's `owner_unrouted` check
    reads. Every unit the owner map admitted or attached a test to, on this
    sweep or an earlier one, is still in `ordered` after the standing-flat
    exclusion and the crash scope, and this sweep owes it a writer: it is in
    the dispatch batch, or the rank left it out on the sweep that meets its
    rotation bound (`dispatch_rank.starved`).

    This sweep's new admits alone hid an owner admitted the sweep before
    (`render`, run `486447-5207101c`, unwritten from fill 3 to fill 8). The
    whole frontier, the next input, read every owner the rank left past the
    cap within its bound. That is the rank's promise, not a routing hole:
    measured 2026-10-17, 214 of 214 recorded hits were such owners, and run
    `3495074-71c576c7` fill 1 (2026-10-20) printed `HEALTH ‼ … owner_unrouted`
    for owners the rank had left out under a 7-sweep bound. A unit the
    exclusions removed has left `ordered` and is not checked."""
    have = frozenset(admitted)
    owed = frozenset(batch) | frozenset(starved)
    return frozenset(k for k in ordered if k in have and k in owed)


def withholding_paired_tests(
    paired: Collection[str],
    *,
    unpaid_fills: Mapping[str, int],
    unpaid_grace: int = DEFAULT_UNPAID_GRACE,
) -> frozenset[str]:
    """Of the leftovers a hop pair covers, the ones that still withhold
    their coverage owners — those whose pair has an UNSPENT buy.

    ``paired`` is every leftover whose eligible units intersect a
    cross-file pair's members. Withholding all of them, for as long as the
    pair exists, is a lock and not a refusal to spend: the release
    condition is that the leftover stops having a pair, and no fill can
    cause that — the pair is re-derived from the same attribution every
    iteration, and a pair that REVERTS keeps its hop. Run
    `1158170-ce307ee7` (2026-09-15) measured the executed slice of its
    three leftovers at 29 units and dispatched 3, at both of its
    iterations, with every call of both fills spent inside the pair's two
    files and the whole route mix returning wrong bodies.

    So the withholding is earned, not free, and it is earned by the same
    bound the schedule already uses to lapse a route's exemptions
    (`DEFAULT_UNPAID_GRACE`): while the pair has been the test's candidate
    for fewer than `unpaid_grace` fills without paying it, under this
    oracle, it is still the paying fill and the coverage spine is the
    spend that cannot pay — the ordering defect that earned this clause
    (run `675471-0f3105f2`, 2026-09-10: 26 coverage locators admitted and
    their never-served per-fn opened before the pair). Past that bound the
    premise is refuted by the pair's own record and the leftover admits
    its measured owners, and it stays lapsed until a pair row pays THAT
    test.

    ``unpaid_fills`` is :func:`fill_schedule.unpaid_pair_fills`: one clock
    over every fill the pair was the test's candidate, opened or deferred.
    Two clocks compared with the grace apart (graded buys; fills with no
    open) re-armed the withholding whenever the pair OPENED before the
    buys reached the bound — run `486447-5207101c` withheld `test_if` again
    at fill 6 after lapsing at fills 4 and 5. A test with no recorded
    count reads as 0 — a pair nobody has a row for has not been a
    candidate, so it keeps the withholding. `unpaid_grace <= 0` disables
    the lapse and restores the unconditional withholding.

    This does not touch the ORDERING the 675471 complaint was really
    about: `run_blocks` still waits every non-pair run that shares the
    pair's leftover tests or members, so an admitted owner's route opens
    after the pair, not before it.
    """
    if unpaid_grace <= 0:
        return frozenset(paired)
    return frozenset(
        t for t in paired if int(unpaid_fills.get(t, 0)) < unpaid_grace)


def attributed_cohorts(
    frontier: Mapping[FnLocator, Collection[str]],
    eligible: Mapping[FnLocator, Collection[str]],
) -> dict[str, set[FnLocator]]:
    """Each failing test's attributed units: the cohort the owner admission,
    the pair withholding and the attributed-write set all read.

    ``frontier`` is the attribution before the exhaustion re-entry filter;
    ``eligible`` is what stayed dispatchable plus what the loop added after
    that filter (crash-blamed units). The cohort is their union. Built from
    ``eligible`` alone, a test whose last attributed unit was skipped as
    exhausted had an EMPTY cohort, which :func:`escalate_to_measured_owners`
    reads as "no attribution" and skips — while its own rule counts an
    exhausted unit as done. Run `3040-51eab48c` (2026-10-02, cluster
    `stable`): the one leftover's implementer exhausted at iter 3, the
    owners admitted at iter 2 left the frontier mid-pass, neither the lapse
    nor the withhold line printed, and iter 4 spent a no-other-move
    re-admission to get the same 17 owners back at iter 5. On iter 3's own
    owner map the eligible-only cohort admits 0 units and this one 19.

    A unit the frontier holds and a filter withheld from dispatch (blocked
    behind a stub, narrowed away on a red build) is in the cohort unserved,
    so it holds the admission until it is served or unable."""
    out: dict[str, set[FnLocator]] = {}
    for src in (frontier, eligible):
        for loc, tests in src.items():
            for t in tests:
                out.setdefault(bare_test_name(t), set()).add(loc)
    return out


def escalate_to_measured_owners(
    *,
    owners: Mapping[str, Collection[FnLocator]],
    units_by_test: Mapping[str, Collection[FnLocator]],
    graded: Collection[FnLocator],
    unable: Collection[FnLocator],
    already_owned: Collection[FnLocator],
    permanently_exhausted: Collection[FnLocator] = (),
    served: Collection[FnLocator] = (),
    paired_tests: Collection[str] = (),
    seen_tests: Collection[str] = (),
) -> dict[FnLocator, set[str]]:
    """Admit coverage-slice owners once every attributed-or-escalated unit
    has been served (or is already unable to pay).

    ``seen_tests`` are the owed red tests this sweep's sight observed
    executing (`test_sight`). For one of them an EMPTY cohort is served:
    the sight removed every edge it had as a guess, nothing attributed is
    left to wait on, and its measured owners are the units it was observed
    running. Any other test with an empty cohort still admits nothing.

    Grade unanimity cannot fire: run ``9872-51b20f30`` (2026-09-01) served
    the whole 30-unit cohort from iter 1 (floor-seeded ``_served_at``,
    ``never_served=[]``) while ``latest_by_unit`` FLAT stayed at 10, so
    ``phase_c.attribution.measured_owners`` emitted zero times and the
    loop re-bought the escalation product for three full passes. Service
    is recorded for a landing, a revert, an INFERRED return, and a
    sibling-pass proven alike — no grade class can hold the fire.

    ``served`` is the kill-safe fill-return set (``_served_at`` keys).
    Dead / red-dead still count as exempt-from-service so a never-opened
    dead product member cannot hold the fire (the 451330 ``write_str``
    hole). ``graded`` does not fire; it only refuses re-admission of a
    unit already graded flat. Admissions are owner-map units minus dead
    minus already-owned minus permanently exhausted minus graded. No
    live test or unit name is a parameter.

    ``paired_tests`` are leftovers whose attribution already resolved a
    hop pair (dispatcher plus implementer) AND whose pair still has an
    unspent buy. Those tests do not admit coverage-spine owners: the pair
    is the paying fill, and widening after the pair members were served is
    the spend that cannot pay (run ``675471-0f3105f2``, 2026-09-10: the
    three format leftovers had a hop pair; measured-owners then admitted
    26 coverage locators and the never-served per-fn of those locators
    opened before the pair). The caller earns that set through
    :func:`withholding_paired_tests`, which drops a leftover once the
    pair's own ledger rows refute the premise — without the lapse this
    skip is a permanent lock, since nothing a fill does makes a leftover
    stop having a pair. Empty (the default) preserves every existing fire.
    """
    already = frozenset(already_owned)
    out: dict[FnLocator, set[str]] = {}
    for tname, loc in _admissible_owners(
            owners=owners, units_by_test=units_by_test, graded=graded,
            unable=unable, permanently_exhausted=permanently_exhausted,
            served=served, paired_tests=paired_tests, seen_tests=seen_tests):
        if loc not in already:
            out.setdefault(loc, set()).add(tname)
    return out


def attach_to_eligible_owners(
    *,
    owners: Mapping[str, Collection[FnLocator]],
    units_by_test: Mapping[str, Collection[FnLocator]],
    eligible: Mapping[FnLocator, Collection[str]],
    graded: Collection[FnLocator],
    unable: Collection[FnLocator],
    permanently_exhausted: Collection[FnLocator] = (),
    served: Collection[FnLocator] = (),
    paired_tests: Collection[str] = (),
    seen_tests: Collection[str] = (),
) -> dict[FnLocator, set[str]]:
    """The measured owners already on the frontier that a test the
    admission would admit them for is not yet aimed by.

    :func:`escalate_to_measured_owners` skips an owner that is already
    eligible — it is not a new unit — and that skip also dropped the TEST:
    the owner stayed aimed at the tests it was admitted for first, and a
    test whose withholding lapsed later was put on no run that writes the
    body its own coverage names. Run `486447-5207101c` (2026-10-07):
    `src/ensure.rs::render` was admitted at iter 6 for six `ensure!` tests;
    ten more lapsed their pair withholding at iter 7, every one executes
    `render`, and the fill-7 file run on `render` aimed at the first five
    only (`test_if` among the missing).

    Same per-test gate as the admission, so the two cannot disagree about
    which tests are measured-owner tests; the answer is only the tests an
    eligible owner does not already carry.
    """
    out: dict[FnLocator, set[str]] = {}
    for tname, loc in _admissible_owners(
            owners=owners, units_by_test=units_by_test, graded=graded,
            unable=unable, permanently_exhausted=permanently_exhausted,
            served=served, paired_tests=paired_tests, seen_tests=seen_tests):
        if loc in eligible and tname not in eligible[loc]:
            out.setdefault(loc, set()).add(tname)
    return out


def _admissible_owners(
    *,
    owners: Mapping[str, Collection[FnLocator]],
    units_by_test: Mapping[str, Collection[FnLocator]],
    graded: Collection[FnLocator],
    unable: Collection[FnLocator],
    permanently_exhausted: Collection[FnLocator],
    served: Collection[FnLocator],
    paired_tests: Collection[str],
    seen_tests: Collection[str] = (),
) -> list[tuple[str, FnLocator]]:
    """(test, owner) pairs past the measured-owner gate: the test is not
    withheld by a pair, its attributed cohort is served or unable to pay
    (an empty cohort counts as served only for a seen test), and the owner
    is neither unable, exhausted nor graded flat."""
    served_keys = frozenset(served)
    unable_set = frozenset(unable)
    exhausted = frozenset(permanently_exhausted)
    graded_set = frozenset(graded)
    paired = frozenset(paired_tests)
    seen = frozenset(str(t).rsplit("::", 1)[-1] for t in seen_tests)
    out: list[tuple[str, FnLocator]] = []
    for tname, owner_set in owners.items():
        if tname in paired:
            continue
        cohort = frozenset(units_by_test.get(tname, ()))
        if not cohort and str(tname).rsplit("::", 1)[-1] not in seen:
            continue
        done = ((cohort & served_keys) | (cohort & unable_set)
                | (cohort & exhausted))
        if not cohort <= done:
            continue
        for loc in owner_set:
            if loc in unable_set or loc in exhausted or loc in graded_set:
                continue
            out.append((tname, loc))
    return out
