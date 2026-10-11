"""One iteration's slice of the TDD frontier, and the records around it.

WHY A SLICE. The TDD sweep is the loop's only measurement of the tree, and it
is nearly free (`rc=101 (0.4s)`). What separates two sweeps is the fill batch
between them, and that batch was the WHOLE frontier: run `669347-cf33eae3`
(2026-08-15) dispatched 55 impl blocks plus 9 file units in one batch and
logged one `phase_c.tdd.iter` in 4h17m. Slicing the frontier makes the loop
look more often at the same model cost.

What this module keeps: the batch record (:class:`DispatchBatch`), the crash
evidence split by provenance (:class:`CrashCohort`), the claim order handed to
the routes (:func:`dispatch_ranks_of`) and the standing-flat exclusion
(:func:`drop_standing_flat`). The ORDER of the frontier and the batch itself
are `dispatch_rank`'s: one rank, one take. The reorderings that used to live
here (blocker counts, the escalation product, producers, service buckets,
crash evidence) and the pass cursor that subtracted what a pass had already
offered each answered the same question on a different scale and undid one
another; their content is the rank's key now.

THE PASS IS THE UNIT THE STALL BUDGET IS DENOMINATED IN. `TDD_MAX_STALLS` and
the flat-score guard count completed traversals, and a capped batch covers
only part of the frontier, so only the sweep after a COMPLETED pass may
charge them. A pass opens over the frontier as it stood (its cohort) and
closes when every cohort member has been traversed or has left the frontier
(`dispatch_rank.take`). Units arriving mid-pass are ranked like any other;
they belong to the next cohort. Run `1692702-94e6163a` (2026-08-16) is why
the cohort exists: its frontier went 35 -> 57 -> 63 under a pass whose
closure condition was "every unit currently in the frontier", and it closed
nothing in 3h36m.
"""
from __future__ import annotations

from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass, field

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


@dataclass(frozen=True, slots=True)
class CrashCohort[K: Hashable]:
    """The units one sweep's crash evidence names, split by PROVENANCE.

    Three sets, each the output of a different mechanism in the driver, kept
    apart because the force they warrant differs (`dispatch_rank.Crash`):

    `blamed` — the localizer's ORIGIN blame: the frame a detector placed the
    fault at (`blamed_locators`). An observation.

    `sited` — every key carrying evidence anchored on a fault SITE the sweep
    produced: all frames of the localized reports (access and origin, so the
    innocent caller that routes into the fault sees the report too), the
    producer candidates one call from a blamed frame, and the vanished test's
    own subject when nothing could be localized. Each is bounded by one fault:
    a report has a handful of in-crate frames, a frame has a handful of
    callees, a test has one subject. `blamed` is a subset of it.

    `chain` — the CHAIN-DERIVED inference: the crashed tests' attributed fns
    plus the producers of their types, synthesized when an origin-capable
    detector looked and placed nothing in-crate. Bounded by the crate, not by
    the fault: 63 fns on run `1577437-c93d5c26` iter 3 (2026-09-06), 67 on
    `263217-d41a5bb5`, 50 on `1064759-0f23fb68`.

    A key may sit in more than one set (a chain fn that is also a frame);
    `grades` and the rank (`dispatch_rank.Crash`) resolve that per key.
    """

    blamed: frozenset[K] = field(default_factory=frozenset)
    sited: frozenset[K] = field(default_factory=frozenset)
    chain: frozenset[K] = field(default_factory=frozenset)

    @property
    def keys(self) -> frozenset[K]:
        """Every unit carrying crash evidence this sweep, any provenance."""
        return self.blamed | self.sited | self.chain

    @property
    def chain_only(self) -> frozenset[K]:
        """The inferred cohort proper: chain keys no fault site names."""
        return self.chain - self.sited - self.blamed

    @property
    def grades(self) -> dict[K, int]:
        """Every crash-evidenced key with its provenance grade: 0 an observed
        faulting frame, 1 chain-derived inference, 2 any other site evidence.
        A key in both the chain and the sited set grades as the chain does
        (1): an inference beside a site key must not outrank the origin
        blame. Membership is what the driver reads (the declined-set
        retraction, the red-dead exemption, the cluster-failed gate); the
        rank reads the provenance (`dispatch_rank.Crash`)."""
        return {
            **dict.fromkeys(self.sited, 2),
            **dict.fromkeys(self.chain, 1),
            **dict.fromkeys(self.blamed, 0),
        }


def dispatch_ranks_of[K: Hashable](ordered: Sequence[K]) -> dict[K, int]:
    """The engine's pool ranks, read off the driver's final dispatch order.

    `dispatch_ranks` is what the fill engine re-sorts its pool on after the
    batch is taken (`rank_stubs_by_blocker_counts`, and the fn order inside
    an impl block), so a run claims its targets in rank order and a share
    that runs out cuts the lowest-ranked ones. The driver has already
    decided the order (`dispatch_rank.rank`). A second ranking built beside
    it is a second answer to the same question, and the two disagree exactly
    where the second one loses information.

    The measurement, run `311306-c8624d12` (2026-09-22). The rank map was the
    blocker histogram with every crash-evidence unit REPLACED by one value per
    grade. All twelve units in the batch were frame-sited at the same grade,
    so all twelve got rank 46, and the pool fell through to its `(file, fn)`
    tiebreak. The fn holding 45 failing tests at its stub started after
    `context.rs` and `ensure.rs` units holding 11 and 14, and the fill's
    premise gate ends a fill at the first keep that moves the ground, so only
    the first four claims ever ran: iter 1 filled the two alphabetically first
    files and kept four bodies whose tests the 45-test stub still held. The
    driver's order had that unit first the whole time (`never_served` on the
    `phase_c.dispatch.service_order` event).

    Rank is position: the head of `ordered` gets `len(ordered)`, the tail 1.
    A unit the engine is handed that is not in `ordered` (a pair member the
    batch cut dropped) gets no entry and sorts after every ranked unit. First
    occurrence wins if a key repeats."""
    out: dict[K, int] = {}
    n = len(ordered)
    for i, k in enumerate(ordered):
        out.setdefault(k, n - i)
    return out


def drop_standing_flat[K: Hashable, V](
    ordered: Sequence[tuple[K, V]],
    flat: Collection[K],
    exempt: Collection[K] = (),
) -> list[tuple[K, V]]:
    """Remove units whose standing claim is FLAT from the dispatch list.

    The service bucket only SINKS flats (`Service.FLAT`). Under cap 12 a 30-unit
    frontier still spends the leftover slots — and after a completed
    traversal, the leftover IS the flats. Run `9872-51b20f30` re-dispatched
    the 27-unit escalation product three times while never-dispatched
    measured owners existed. `rebuy_hits` cannot be this owner: it is
    gated off after a code move and skips the same `run_id`, so this
    process's own flats (the live spend) never appear. Crash-blamed (or
    otherwise exempt) units stay: live evidence outranks a prior empty
    delta. Empty `flat` is a no-op.
    """
    if not flat:
        return list(ordered)
    skip = set(flat) - set(exempt)
    if not skip:
        return list(ordered)
    return [item for item in ordered if item[0] not in skip]
