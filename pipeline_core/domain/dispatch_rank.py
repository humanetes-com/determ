"""The one ordering of the TDD frontier, the batch it yields, and the count
that keeps it fair.

WHY THIS EXISTS. The frontier used to reach the batch through about ten
reorderings and filters, each written for one run and each able to undo the
one before it: `order_frontier`, then blocker counts, then the escalation
product ahead, then producers before their callers, then the service buckets,
then crash evidence, then the owed share, then the pass cursor with its
exemption. Several of them said "crash promotion runs after this, so an
observed fault keeps the very front"; the owed share, added later, ran after
crash promotion and moved a crash-graded owed unit into the owed half, where
it was sorted by `(tier, -cut streak, name)`. Every owed row read tier -1 and
streak 0 on the sweep a unit first became owed, so the name decided. Run
`857717-812ee44c` (2026-10-10, cluster `stable`, iter 5): 20 owed units
against a cap of 12, and the two stubs a red test's first failure sat in,
`src/error.rs::object_drop_front` and `src/wrapper.rs::source`, sorted 15th
and 20th, out of the batch. `scripts/held_stub_batch_fixture.py` recomputes
that batch from the run's events and reproduces the recorded one on all five
sweeps. A unit the batch did admit and a run then left unclaimed when its
share ran out (fill 4, `object_drop_front`) counted as served for the owed
share's starvation guard, so that guard never fired either.

THE RANK. One key per unit, built from typed evidence:

- class `HELD`: a still-stubbed unit some red test's first failure sits in
  (`held_stubs.HeldStubs`), or an origin or exhaustion culprit
  (`crash_site.blocker_culprit_tests`). Every one is in the batch,
  whatever the cap, ordered by how many tests it blocks.
- class `EVIDENCED`: the unit has something no prompt has asked it. That is
  evidence it is owed and no prompt has asked yet (`candidate_tiers`), a
  crash's fault site naming it (a localized frame, a producer one call from
  it, a synthesized subject), or no body ever served to it
  (`Service.NEVER`).
- class `OTHER`: everything else. That is a served body with nothing new to
  show (a re-buy), and a dead or red-dead unit, which cannot move a red
  verdict this sweep and becomes held the sweep a red test reaches it while
  it is a stub. A crash's chain-derived inference leads it.

WHY A NEVER-SERVED UNIT IS EVIDENCED. The streak below rotates units only
inside a class, and `EVIDENCED` does not drain: a unit stays owed until a
body lands under its test's fingerprint or the misses run out, and the
fingerprint moves with the tree. A never-served unit in `OTHER` therefore
got no slot on any sweep where the held and owed units filled the cap. Runs
`2157238-210637e6`, `3163270-ca5a787c` and `35210-6d56e8c9` (2026-10-11/12,
cluster `stable`): 2 held and 22 owed against a cap of 12, and 44
never-served units that were neither held nor owed got 0 slots on four
consecutive sweeps, streaks up to 3. They were `construct`, `vtable`, every
`object_*` vtable entry, every `src/ptr.rs` body and `src/wrapper.rs::fmt`,
all still stubs. On the run that climbed (`857717-812ee44c`), the
never-served order filled the same units and they paid +10, +13 and +39.
A class of its own ahead of the owed units would not drain either:
`service_of` keeps a stub `NEVER` while its offers fail to land. Inside one
class the streak bounds both, every member offered within
`ceil(|EVIDENCED| / max(cap - |held|, 1))` sweeps of claims.

Within each class the first key is the unclaimed streak, the number of
sweeps the unit has been on the frontier since a turn last counted
(`streak_after_turn`). A unit the cap cut, a run's share cut or a premise
refusal skipped had no turn and leads the next sweep. An offered unit
resets, so the frontier rotates and a pass can complete when no body
changed. A fault site whose body did not land does not reset the first
time — the open was taken back — which is what keeps it at the head of
its class. Counting that open reset the two fault-site units on run
`2157238-210637e6` (iter 3) to zero after the file route peeled them, and
they sat 15th and 16th, past the cap, behind units no route had opened.
A later peel does reset it, so a fault that never lands cannot hold the
batch. That bound is what the pass cursor and the owed share's half split
each tried to give and neither did for every unit. After the streak: tests
held (`HELD`); fault site (blamed, then sited), owed tier with an un-owed
unit after every tier, and service bucket (`EVIDENCED`);
the chain inference, then the service bucket (`OTHER`: never served, served,
served and last graded flat, red-dead, dead). Then the escalation's producer order: product units first,
table builders, then callees before their callers. Then the static order
(`order_frontier`: panic depth, callers, leaf-name frequency). The name
decides only an exact tie.

WHAT A PASS STILL MEANS. The terminators count completed traversals
(`flat_score`, the plateau budget), so the pass survives as accounting
(:func:`take`): it opens over the frontier as it stands (its cohort) and
closes when every cohort member has been traversed or has left the frontier.
It no longer decides the order.
"""

from __future__ import annotations

from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from pipeline_core.domain.dispatch_batch import DispatchBatch

__all__ = [
    "Crash",
    "asked_unchanged",
    "RankClass",
    "absence_notice",
    "Service",
    "UnitFacts",
    "Withheld",
    "next_streak",
    "off_batch_reasons",
    "streak_after_turn",
    "rank",
    "rotation_bound",
    "service_of",
    "starved",
    "take",
]


class RankClass(IntEnum):
    HELD = 0
    EVIDENCED = 1
    OTHER = 2


class Service(IntEnum):
    """How the unit's current body has been served (`service_of`)."""

    NEVER = 0
    SERVED = 1
    FLAT = 2
    RED_DEAD = 3
    DEAD = 4


class Crash(IntEnum):
    """A crash's provenance for the unit. `BLAMED` and `SITED` are anchored
    on a fault the sweep produced; `CHAIN` is the inference drawn when no
    frame was in the crate, bounded by the crate rather than by the fault
    (63 units on run `1577437-c93d5c26` iter 3), so it ranks in `OTHER`."""

    BLAMED = 0
    SITED = 1
    CHAIN = 2


def service_of[K: Hashable](
    key: K,
    *,
    served: Collection[K],
    flat: Collection[K],
    dead: Collection[K] = (),
    red_dead: Collection[K] = (),
    hollow: Collection[K] = (),
) -> Service:
    """The unit's service bucket.

    Dead first: a body no test executes cannot move a verdict. Red-dead next:
    no failing test executes it, so a rewrite cannot move a red verdict this
    sweep (`src/lib.rs::Ok`, generic and `#[inline]`, is absent from the
    profile and can never be dead). A still-stubbed unit is never served
    whatever its stamp says: the stamp is about a body no longer on disk
    (run `3453127-045b05b1`: `vtable` carried an earlier process's stamp, was
    a stub again, and 82 of 141 failing tests stopped inside it)."""
    if key in dead:
        return Service.DEAD
    if key in red_dead:
        return Service.RED_DEAD
    if key in hollow or key not in served:
        return Service.NEVER
    if key in flat:
        return Service.FLAT
    return Service.SERVED


#: The owed-tier key of a unit nothing owes: after every real tier.
_UNOWED_TIER = 1 << 30


@dataclass(frozen=True, slots=True)
class UnitFacts:
    """Everything the rank reads about one frontier unit."""

    #: Position in the static order (`order_frontier`). Unique per unit.
    static_index: int
    #: How the unit's current body has been served (`service_of`). No
    #: default: `NEVER` puts a unit in `EVIDENCED`, so a constructor that
    #: forgot to ask would promote it.
    service: Service
    #: Red tests whose first failure sits in this still-stubbed unit.
    held_tests: int = 0
    crash: Crash | None = None
    #: The narrowest tier this unit is owed on; None when nothing owes it.
    owed_tier: int | None = None
    #: Member of the escalation's product (its producers or measured owners).
    escalated: bool = False
    #: Constructs a crate nominal type (`installable_fns.producer_dispatch_ranks`).
    builder: bool = False
    #: Called by another escalated unit through a resolved call.
    callee: bool = False
    #: Sweeps on the frontier since a turn last counted (`streak_after_turn`).
    unclaimed: int = 0
    #: An origin or exhaustion culprit (`blocker_culprit_tests`). Held even
    #: when the crash recorded no test name. A family sibling widened onto
    #: the blame is not one: it stays `Crash.BLAMED` inside `EVIDENCED`.
    culprit: bool = False
    #: Tests this unit blocks: stub holds union the crash's tests. The held
    #: key orders by this when it is non-zero, and by `held_tests` otherwise,
    #: so a caller that only counted stub holds keeps that order.
    blocked_tests: int = 0

    @property
    def rank_class(self) -> RankClass:
        if self.held_tests > 0 or self.culprit:
            return RankClass.HELD
        if self.owed_tier is not None or self.crash in (Crash.BLAMED, Crash.SITED) or self.service is Service.NEVER:
            return RankClass.EVIDENCED
        return RankClass.OTHER

    def _producer(self) -> tuple[int, int, int]:
        return (0 if self.escalated else 1, 0 if self.builder else 1, 0 if self.callee else 1)

    def key(self) -> tuple[int, ...]:
        cls = self.rank_class
        if cls is RankClass.HELD:
            blocked = self.blocked_tests or self.held_tests
            return (int(cls), -self.unclaimed, -blocked, self.static_index)
        if cls is RankClass.EVIDENCED:
            # A fault site leads; a chain inference is not one and keys with
            # no crash. An un-owed unit keys after every owed tier: the
            # driver clamps tiers to 0, so an un-owed never-served unit keyed
            # 0 would win on service over an owed served one.
            fault = int(self.crash) if self.crash in (Crash.BLAMED, Crash.SITED) else int(Crash.CHAIN)
            tier = self.owed_tier if self.owed_tier is not None else _UNOWED_TIER
            return (int(cls), -self.unclaimed, fault, tier, int(self.service), *self._producer(), self.static_index)
        return (
            int(cls),
            -self.unclaimed,
            0 if self.crash is Crash.CHAIN else 1,
            int(self.service),
            *self._producer(),
            self.static_index,
        )


def asked_unchanged[K: Hashable](
    facts: Mapping[K, UnitFacts], asked: Collection[K],
) -> frozenset[K]:
    """The units that leave this sweep's frontier: class `OTHER`, a body
    already served, and opened before under the own fingerprint they would
    be asked under now (`asked`, computed by the caller from
    `evidence_premise.unit_ask_fingerprint`: own body + the words of the red
    tests the unit is aimed at).

    The cap is a ceiling, not a quota. Every sweep used to fill it, and the
    unclaimed streak rotated served units nothing owed back in under
    unchanged evidence, each pulling a file run, an impl run and a per-fn
    target with it: run `3076-051d385a` iter 3 (2026-10-13) gave six of
    twelve slots to such units at 148/151.

    The class decides what is never touched: a held stub, an owed unit, a
    crash fault site and a never-served unit are `HELD`/`EVIDENCED`, and
    they keep their slots whatever `asked` says. That is the climb: a
    never-served stub ahead of the current failure point is on no red slice
    yet and is the layer fill that paid +10, +13 and +39 on
    `857717-812ee44c`. `asked` holding a unit whose fingerprint was empty
    (nothing observed) is the caller's error; such a unit must stay."""
    have = frozenset(asked)
    return frozenset(
        k for k, f in facts.items()
        if k in have and f.rank_class is RankClass.OTHER
        and f.service is not Service.NEVER)


def rank[K: Hashable](facts: Mapping[K, UnitFacts]) -> list[K]:
    """Every unit of `facts`, best first."""
    return sorted(facts, key=lambda k: (facts[k].key(), str(k)))


def take[K: Hashable](
    ranked: Sequence[K],
    held: Collection[K],
    cap: int,
    *,
    cohort: Collection[K] | None,
    traversed: Collection[K],
) -> DispatchBatch[K]:
    """The batch for this sweep, and where it leaves the open pass.

    Every held unit is taken; the cap bounds the rest, which always keep at
    least one slot so a frontier that holds more stubs than the cap still
    offers one other unit a sweep. A cap of 0 or less takes everything.

    `cohort` is the frontier the open pass was opened over (None: open one
    over `ranked`), `traversed` what it has already traversed. The pass
    completes when this batch covers every cohort member still on the
    frontier that it has not traversed, which includes the case where
    nothing of the cohort is left to traverse: the caller then opens the
    next pass at the next sweep. A unit re-offered on live evidence after it
    was traversed does not hold the pass open, so a fault blaming more units
    than the cap cannot leave the terminators that count completed passes
    with nothing to count."""
    held_set = set(held)
    held_in = [k for k in ranked if k in held_set]
    rest = [k for k in ranked if k not in held_set]
    if cap <= 0:
        keys = tuple(ranked)
    else:
        room = max(cap - len(held_in), 1 if rest else 0)
        keys = (*held_in, *rest[:room])
    live = set(ranked)
    cohort_live = live if cohort is None else live & set(cohort)
    seen = set(traversed) & cohort_live
    pending = cohort_live - seen
    return DispatchBatch(
        keys=keys, completes_pass=not (pending - set(keys)), attempted_before=len(seen), frontier_size=len(ranked)
    )


def rotation_bound(class_size: int, held: int, cap: int) -> int:
    """Sweeps within which every `EVIDENCED` unit is offered, when the runs
    claim what they are given: the class over the slots :func:`take` leaves
    after the held units, never fewer than one slot."""
    if cap <= 0 or class_size <= 0:
        return 1
    return -(-class_size // max(cap - held, 1))


def starved[K: Hashable](
    facts: Mapping[K, UnitFacts],
    batch: Collection[K],
    cap: int,
) -> dict[K, int]:
    """`EVIDENCED` units this batch leaves out on the sweep that meets the
    rotation bound, with the streak read at rank time.

    The streak climbs after the batch is chosen. A unit already at
    `bound - 1` that this batch does not take has then been left out for
    `bound` sweeps. A class that fits in one batch has bound 1, so a
    streak of 0 left out is already the miss. The class's size moves
    between sweeps, so the bound is read on this sweep's class. Empty
    when the rank keeps its promise."""
    batch_set = set(batch)
    evidenced = [k for k, f in facts.items() if f.rank_class is RankClass.EVIDENCED]
    held = sum(1 for f in facts.values() if f.rank_class is RankClass.HELD)
    bound = rotation_bound(len(evidenced), held, cap)
    # Rank time is before this sweep's climb, so the breaking streak is
    # one below the bound. `rotation_bound` never returns less than 1.
    missed = bound - 1
    return {k: facts[k].unclaimed for k in evidenced if k not in batch_set and facts[k].unclaimed >= missed}


def next_streak[K: Hashable](
    streak: Mapping[K, int],
    frontier: Iterable[K],
    reset: Collection[K],
) -> dict[K, int]:
    """The unclaimed streak after a fill.

    `reset` is the units whose turn counted. Each of those goes to 0.
    Every other unit still on the frontier climbs by one. Units that left
    the frontier are dropped. Which turns count is :func:`streak_after_turn`."""
    reset_set = set(reset)
    return {k: (0 if k in reset_set else streak.get(k, 0) + 1) for k in frontier}


def streak_after_turn[K: Hashable](
    streak: Mapping[K, int],
    frontier: Iterable[K],
    offered: Collection[K],
    landed: Collection[K],
    fault_sites: Collection[K],
    retried: Collection[K],
) -> tuple[dict[K, int], frozenset[K]]:
    """The streak after one fill, and the fault sites already given a stay.

    An offered unit resets, so the frontier rotates when a batch changes
    no bytes and a pass can still complete. A share cut, a premise refusal
    and a lost answer are not in `offered`: they had no turn and climb.

    A fault site (`Crash.BLAMED` or `Crash.SITED`, never a chain inference)
    whose body did not land had its turn taken back. The first time, it is
    left out of the reset and climbs with the units no route opened, so
    crash provenance still puts it at the head of the class. `retried`
    records that stay. The next peel resets it like any other offered unit,
    so a fault that never lands cannot hold the cap. A landing forgets the
    stay: the next peel of a body that did land earns one more.

    A body that landed resets even when no route opened the unit (a
    passenger of a wider write). The returned set is the stays still
    remembered for units on this frontier."""
    front = list(frontier)
    front_set = set(front)
    offered_set = set(offered) & front_set
    landed_set = set(landed) & front_set
    stay = (offered_set & set(fault_sites)) - landed_set - set(retried)
    reset = (offered_set - stay) | landed_set
    remembered = ((set(retried) - landed_set) | stay) & front_set
    return next_streak(streak, front, reset), frozenset(remembered)


@dataclass(slots=True)
class Withheld[K: Hashable]:
    """Why each unit that reached the frontier is not ranked this sweep.

    The membership filters (exhaustion, consumer deferral, compile-red
    narrowing, standing flat, crash scope, cfg) each note what they removed.
    A held unit may only be withheld as cfg-inactive: every filter is handed
    the held set as exempt, and :meth:`held_violations` reports any that was
    removed anyway, for a HEALTH line rather than a silent drop."""

    reasons: dict[K, str] = field(default_factory=dict)

    def note(self, units: Iterable[K], reason: str) -> None:
        for u in units:
            self.reasons.setdefault(u, reason)

    def held_violations(self, held: Collection[K]) -> dict[K, str]:
        return {u: r for u, r in self.reasons.items() if u in held and r != "cfg_inactive"}


def off_batch_reasons[K: Hashable](
    units: Iterable[K],
    ranked: Sequence[K],
    batch: Collection[K],
    withheld: Withheld[K],
    cap: int,
) -> dict[K, str]:
    """For each of `units` not in the batch: its rank position when it is
    on the frontier, else the filter that kept it off, else that it was
    never on the frontier.

    A filter note does not outrank the frontier. The standing-flat
    exclusion notes a unit and the owed admission then puts that unit
    back; the note stays. The unit is ranked, so the reason it missed
    the batch is the position."""
    pos = {k: i for i, k in enumerate(ranked, 1)}
    batch_set = set(batch)
    out: dict[K, str] = {}
    for u in units:
        if u in batch_set:
            continue
        if u in pos:
            out[u] = f"rank {pos[u]} past the cap of {cap}"
        elif u in withheld.reasons:
            out[u] = f"withheld: {withheld.reasons[u]}"
        else:
            out[u] = "not on the frontier"
    return out


def absence_notice[K: Hashable](
    watched: Mapping[K, str],
    urgent: Mapping[K, str],
) -> dict[K, str]:
    """Reasons for the log's absence clause, `urgent` first.

    The clause prints a fixed number of names. A unit the rotation has
    already failed to reach has to be among those names. Appending it
    after a full watch leaves it in the unprinted tail."""
    out = dict(urgent)
    for key, why in watched.items():
        out.setdefault(key, why)
    return out
