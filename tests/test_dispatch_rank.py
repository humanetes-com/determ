"""The one rank, the batch it yields, and the count that keeps it fair
(`domain/dispatch_rank`).

Every reordering this module replaced was written for a recorded run, and its
instance is replayed here against the rank: the blocker count, the
escalation product ahead of new accessors, the builders before their
delegators, the service buckets, crash evidence ahead of the static order,
one fault's family dispatched together, a 63-unit chain cohort offered once
each before any twice, the owed share of run `39786-fd38e72f` fill 4, and the
held stubs of run `857717-812ee44c` the owed share cut. The deleted pieces
are kept here as frozen copies where a negative control needs them.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from pipeline_core.domain.dispatch_batch import DEFAULT_MAX_DISPATCH, DispatchBatch
from pipeline_core.domain.dispatch_rank import (
    Crash,
    RankClass,
    Service,
    UnitFacts,
    Withheld,
    next_streak,
    off_batch_reasons,
    rank,
    service_of,
    take,
)
from pipeline_core.domain.installable_fns import producer_dispatch_ranks
from pipeline_core.domain.models import FnLocator
from tests._cohort_95878 import _COHORT_ORDER, _COHORT_RECS, _NOT_ESCALATED, _SIX
from tests.test_dispatch_batch import _types


def _static(keys: Sequence[str]) -> dict[str, UnitFacts]:
    return {k: UnitFacts(static_index=i) for i, k in enumerate(keys)}


def _sweeps(frontiers: Sequence[Sequence[str]], cap: int, *,
            facts: Callable[[str, int], UnitFacts] | None = None,
            claim: Callable[[tuple[str, ...]], set[str]] | None = None,
            cohort_mode: bool = True) -> list[DispatchBatch[str]]:
    """The driver's sweep loop over these frontiers: rank on the streak, take
    the batch, close or advance the pass, and update the streak with what the
    runs claimed (the whole batch unless `claim` says otherwise)."""
    streak: dict[str, int] = {}
    traversed: set[str] = set()
    cohort: tuple[str, ...] | None = None
    out: list[DispatchBatch[str]] = []
    for frontier in frontiers:
        def _f(k: str, i: int, now: dict[str, int] = streak) -> UnitFacts:
            base = facts(k, i) if facts else UnitFacts(static_index=i)
            return replace(base, unclaimed=now.get(k, 0))
        ranked = rank({k: _f(k, i) for i, k in enumerate(frontier)})
        if cohort is None and cohort_mode:
            cohort = tuple(ranked)
        b = take(ranked, (), cap, cohort=cohort if cohort_mode else None,
                 traversed=traversed)
        out.append(b)
        if b.completes_pass:
            traversed, cohort = set(), None
        else:
            traversed |= set(b.keys)
        claimed = claim(b.keys) if claim else set(b.keys)
        streak = next_streak(streak, ranked, claimed)
    return out


# --- the batch and the pass -------------------------------------------------

def test_the_batch_is_the_head_of_the_rank_and_does_not_complete_the_pass():
    ordered = [f"u{i}" for i in range(30)]
    b = take(rank(_static(ordered)), (), 12, cohort=None, traversed=())
    assert b.keys == tuple(ordered[:12])
    assert not b.completes_pass and b.capped
    assert b.attempted_before == 0 and b.frontier_size == 30


def test_successive_sweeps_cover_the_whole_frontier_then_close_the_pass():
    """Three sweeps of 12 cover 30 units, every unit is offered by the third,
    and only the third closes the pass, the flag the stall budget charges on."""
    ordered = [f"u{i}" for i in range(30)]
    batches = _sweeps([ordered] * 3, 12)
    offered = [k for b in batches for k in b.keys]
    assert set(offered) == set(ordered)
    assert batches[0].keys == tuple(ordered[:12])
    assert batches[1].keys == tuple(ordered[12:24])
    assert set(ordered[24:]) <= set(batches[2].keys)
    assert [b.completes_pass for b in batches] == [False, False, True]


def test_a_churning_frontier_cannot_starve_its_tail():
    """The frontier is rebuilt every sweep. A unit that arrives mid-pass, and
    the tail behind the head, are both reached: the streak ranks whatever
    waited longest first."""
    first = [f"u{i}" for i in range(20)]
    second = [k for k in first if k not in {"u0", "u1"}] + ["new"]
    batches = _sweeps([first] + [second] * 5, 5)
    assert batches[1].keys == ("u5", "u6", "u7", "u8", "u9")
    later = [k for b in batches[1:] for k in b.keys]
    assert "new" in later and set(second) <= set(later)


def test_a_frontier_under_the_cap_completes_in_one_batch():
    b = take(["a", "b", "c"], (), DEFAULT_MAX_DISPATCH, cohort=None, traversed=())
    assert b.keys == ("a", "b", "c") and b.completes_pass and not b.capped


def test_a_cap_of_zero_takes_the_whole_frontier_in_one_completed_pass():
    ordered = [f"u{i}" for i in range(50)]
    b = take(ordered, (), 0, cohort=None, traversed=())
    assert b.keys == tuple(ordered) and b.completes_pass


def test_a_pass_whose_cohort_is_fully_traversed_completes():
    """Every cohort member filled and left, or was traversed already: the
    pass is done, whatever the batch re-offers on live evidence."""
    b = take(["a", "b"], (), 12, cohort=None, traversed={"a", "b", "gone"})
    assert b.keys == ("a", "b") and b.completes_pass
    ordered = [f"u{i}" for i in range(40)]
    wide = take(ordered, (), 12, cohort=ordered, traversed=ordered)
    assert len(wide.keys) == 12 and wide.completes_pass


def test_a_growing_frontier_never_closes_a_pass_without_a_cohort():
    """THE NEGATIVE CONTROL for the cohort, run `1692702-94e6163a`: with
    closure judged against the frontier as it stands, a frontier growing
    35 -> 57 -> 63 never empties its pending set and neither terminator that
    counts completed passes can fire."""
    sizes = [35, 57, 63, 69, 75]
    batches = _sweeps([[f"u{i}" for i in range(n)] for n in sizes], 12,
                      cohort_mode=False)
    assert [b.completes_pass for b in batches] == [False] * len(sizes)


def test_the_cohort_closes_the_pass_while_the_frontier_grows():
    """Judged against the cohort the pass opened over: 35 units at 12 a sweep
    close on the third, and the arrivals belong to the next cohort."""
    batches = _sweeps([[f"u{i}" for i in range(n)] for n in (35, 57, 63)], 12)
    assert [b.completes_pass for b in batches] == [False, False, True]


def test_a_cohort_unit_that_left_the_frontier_does_not_block_closure():
    b = take(["a", "c"], (), 12, cohort=["a", "b", "c"], traversed={"a"})
    assert b.keys == ("a", "c") and b.completes_pass


def test_cohort_none_is_the_frontier_as_it_stands():
    ordered = [f"u{i}" for i in range(30)]
    for traversed in (set(), {"u0", "u1"}, set(ordered[:29])):
        a = take(ordered, (), 12, cohort=None, traversed=traversed)
        b = take(ordered, (), 12, cohort=ordered, traversed=traversed)
        assert (a.keys, a.completes_pass) == (b.keys, b.completes_pass)


# --- held stubs ---------------------------------------------------------------

def test_every_held_stub_is_in_the_batch_whatever_the_cap():
    """More held stubs than the cap: all of them are taken, and the rest keep
    one slot so a frontier full of stubs still offers one other unit a
    sweep."""
    held = [f"h{i}" for i in range(14)]
    others = [f"u{i}" for i in range(10)]
    facts = {**{k: UnitFacts(static_index=20 + i, held_tests=1)
                for i, k in enumerate(held)},
             **{k: UnitFacts(static_index=i) for i, k in enumerate(others)}}
    b = take(rank(facts), held, 12, cohort=None, traversed=())
    assert set(held) <= set(b.keys)
    assert len(b.keys) == 15 and b.keys[-1] == "u0"


def test_held_stubs_lead_in_order_of_the_tests_they_hold_then_rotate():
    facts = {"small": UnitFacts(static_index=0, held_tests=1),
             "big": UnitFacts(static_index=1, held_tests=54),
             "owed": UnitFacts(static_index=2, owed_tier=0),
             "blamed": UnitFacts(static_index=3, crash=Crash.BLAMED)}
    assert rank(facts) == ["big", "small", "blamed", "owed"]
    # The one cut last sweep goes first among the held.
    facts["small"] = UnitFacts(static_index=0, held_tests=1, unclaimed=1)
    assert rank(facts)[:2] == ["small", "big"]


def _pre_fix_share_batch(crash: list[str], owed: list[str], cap: int, *,
                         cut_streak: dict[str, int], tier_of: dict[str, int],
                         misses: int = 3) -> tuple[list[str], list[str]]:
    """Frozen copy of the deleted `candidate_tiers.share_batch`."""
    owed_sorted = sorted(
        owed, key=lambda u: (tier_of.get(u, 0), -cut_streak.get(u, 0), str(u)))
    owed_set = set(owed_sorted)
    crash_only = [u for u in crash if u not in owed_set]
    if not owed_sorted:
        return list(crash), []
    if cap <= 0 or not crash_only or len(crash_only) + len(owed_sorted) <= cap:
        return [*crash_only, *owed_sorted], []
    starved = [u for u in owed_sorted if cut_streak.get(u, 0) >= misses > 0]
    rest_owed = [u for u in owed_sorted if u not in set(starved)]
    owed_share = min(len(owed_sorted), -(-cap // 2))
    crash_take = max(0, cap - owed_share - len(starved))
    return ([*starved, *crash_only[:crash_take], *rest_owed],
            list(crash_only[crash_take:]))


#: Run `857717-812ee44c`, cluster `stable`, iter 5, from `logs/events.jsonl`:
#: the owed set (`phase_c.tdd.owed`), the first-failure holds
#: (`phase_c.crash.stub_holds`) and the owed units the schedule received
#: (`phase_c.fill.schedule` `inputs.due`). Every owed row read tier -1.
_OWED_857717_ITER5 = [
    "src/chain.rs::len", "src/ensure.rs::__dispatch_ensure",
    "src/ensure.rs::as_str", "src/ensure.rs::new", "src/ensure.rs::write_str",
    "src/error.rs::construct_from_boxed", "src/error.rs::downcast",
    "src/error.rs::downcast_mut", "src/error.rs::into_boxed_dyn_error",
    "src/error.rs::is", "src/error.rs::msg", "src/error.rs::new",
    "src/error.rs::object_boxed", "src/error.rs::object_downcast",
    "src/error.rs::object_drop_front", "src/fmt.rs::debug",
    "src/fmt.rs::display", "src/lib.rs::format_err", "src/ptr.rs::by_mut",
    "src/wrapper.rs::source"]
_HELD_857717_ITER5 = {"src/error.rs::object_drop_front": 1,
                      "src/wrapper.rs::source": 3}
_DUE_857717_ITER5 = set(_OWED_857717_ITER5[:12])


def test_recorded_857717_iter5_held_stubs_lead_the_batch():
    """The plan's sentence: under the rank both held stubs are in the first
    12. Control: the deleted owed share, on the same inputs with every streak
    at 0, reproduces the recorded batch and leaves them out at 15 and 20."""
    crash = sorted(_HELD_857717_ITER5)
    first, after = _pre_fix_share_batch(crash, list(_OWED_857717_ITER5), 12,
                                        cut_streak={}, tier_of={})
    pre = (first + after)[:12]
    assert set(pre) == _DUE_857717_ITER5
    assert (first + after).index("src/error.rs::object_drop_front") == 14
    assert (first + after).index("src/wrapper.rs::source") == 19

    facts = {u: UnitFacts(static_index=i,
                          held_tests=_HELD_857717_ITER5.get(u, 0),
                          crash=Crash.SITED if u in _HELD_857717_ITER5 else None,
                          owed_tier=0)
             for i, u in enumerate(_OWED_857717_ITER5)}
    b = take(rank(facts), set(_HELD_857717_ITER5), 12, cohort=None,
             traversed=())
    assert b.keys[:2] == ("src/wrapper.rs::source",
                          "src/error.rs::object_drop_front")


def test_39786_fill4_shape_every_owed_unit_is_offered_within_the_bound():
    """Run `39786-fd38e72f` fill 4: 13 crash units took all 12 slots and none
    of 26 owed functions was offered. The owed share fixed that with a half
    split and starved its own alphabetical tail. Under the rank every
    evidenced unit, crash or owed, is offered within ceil(39 / 12) sweeps
    when the runs claim what they are given."""
    crash = [f"c{i:02d}" for i in range(13)]
    owed = [f"o{i:02d}" for i in range(26)]

    def facts(k: str, i: int) -> UnitFacts:
        return UnitFacts(static_index=i,
                         crash=Crash.SITED if k in crash else None,
                         owed_tier=0 if k in owed else None)

    bound = math.ceil(39 / 12)
    batches = _sweeps([crash + owed] * bound, 12, facts=facts)
    offered = {k for b in batches for k in b.keys}
    assert set(owed) <= offered and set(crash) <= offered


def test_the_narrowest_owed_tier_leads_the_evidenced_class():
    """Owed functions are asked narrowest tier first; the crash provenance of
    a sited unit comes before the tier, and the streak before both."""
    facts = {"wide": UnitFacts(static_index=0, owed_tier=5),
             "narrow": UnitFacts(static_index=1, owed_tier=0),
             "mid": UnitFacts(static_index=2, owed_tier=3),
             "sited": UnitFacts(static_index=3, crash=Crash.SITED)}
    assert rank(facts) == ["sited", "narrow", "mid", "wide"]
    facts["wide"] = UnitFacts(static_index=0, owed_tier=5, unclaimed=2)
    assert rank(facts)[0] == "wide"


def test_a_share_cut_counts_as_unclaimed():
    """Batch membership is not a claim. A unit the batch admitted and the
    run's share ran out before leads the next sweep. Control: the owed
    share's streak, counted on the batch, gave it 0 (run `857717-812ee44c`
    fill 4, `src/error.rs::object_drop_front`)."""
    streak = next_streak({}, ["a", "b", "c"], claimed={"a"})
    assert streak == {"a": 0, "b": 1, "c": 1}
    batch = ("a", "b")
    pre_fix = {u: (0 if u in batch else 1) for u in ("a", "b", "c")}
    assert pre_fix["b"] == 0 and streak["b"] == 1
    assert next_streak(streak, ["a", "b"], claimed={"b"}) == {"a": 1, "b": 0}


# --- the content of the deleted reorderings ---------------------------------

def test_crash_evidence_is_ranked_ahead_of_the_static_order():
    """Run `1692702-94e6163a`: the five fns a double drop named waited for
    their alphabetical turn. An observed frame first, other site evidence
    next, the chain inference leading everything else."""
    facts = {f"u{i}": UnitFacts(static_index=i) for i in range(6)}
    facts["u5"] = UnitFacts(static_index=5, crash=Crash.BLAMED)
    facts["u4"] = UnitFacts(static_index=4, crash=Crash.CHAIN)
    facts["u2"] = UnitFacts(static_index=2, crash=Crash.SITED)
    assert rank(facts) == ["u5", "u2", "u4", "u0", "u1", "u3"]
    assert rank(_static([f"u{i}" for i in range(5)])) == [f"u{i}" for i in range(5)]


def test_one_fault_s_family_dispatches_together():
    """The family of one fault holds consecutive slots of the batch that found
    it, in the static order; `object_drop_front`, last on the `(file, fn)`
    tiebreak, is in the same batch as `object_drop`."""
    fam = ["context_chain_drop_rest", "context_drop_rest", "drop",
           "object_drop", "object_drop_front"]
    keys = [f"u{i}" for i in range(20)] + fam
    facts = {k: UnitFacts(static_index=i,
                          crash=Crash.SITED if k in fam else None)
             for i, k in enumerate(keys)}
    assert not set(keys[:12]) & set(fam)   # control: the static order's cut
    b = take(rank(facts), (), 12, cohort=None, traversed=())
    assert b.keys[:5] == tuple(fam)


def test_the_escalation_product_and_its_builders_lead_the_rest():
    """Runs `4911`/`2932`: the static order ranks a newly attributed accessor
    first and a cap of 1 parked the producer. The product leads, builders
    before delegators inside it; a crash-blamed unit still leads both."""
    acc, deleg, build, crash = "acc", "deleg", "build", "crash"
    facts = {acc: UnitFacts(static_index=0),
             deleg: UnitFacts(static_index=1, escalated=True),
             crash: UnitFacts(static_index=2, crash=Crash.BLAMED),
             build: UnitFacts(static_index=3, escalated=True, builder=True,
                              callee=True)}
    assert rank(facts) == [crash, build, deleg, acc]
    assert take(rank(facts), (), 1, cohort=None, traversed=()).keys == (crash,)
    del facts[crash]
    assert take(rank(facts), (), 1, cohort=None, traversed=()).keys == (build,)


def test_recorded_95878_cohort_dispatches_the_constructors_in_batch_one():
    """Runs `40938-f4e8e94b`, `4911-9ab8852e`, `95878-aaf5bf1d`: three producer
    passes ran the identical first batch with zero table builders. On the
    recorded cohort the rank's first 12 hold all six `construct*`. Control:
    the recorded order's first 12 hold none of them."""
    recs = [{"file": f, "fn_name": n, "container": c, "body_text": b}
            for f, n, c, b in _COHORT_RECS]
    order = [FnLocator(f, n) for f, n in _COHORT_ORDER]
    esc = frozenset(FnLocator(f, n) for f, n in _COHORT_ORDER
                    if (f, n) not in _NOT_ESCALATED)
    builders, callees = producer_dispatch_ranks(esc, recs, _types())
    ranked = rank({k: UnitFacts(static_index=i, escalated=k in esc,
                                builder=k in builders, callee=k in callees)
                   for i, k in enumerate(order)})
    first = {(k.file, k.fn_name) for k in ranked[:12]}
    assert first >= _SIX, sorted(first)
    assert not (_SIX & {(k.file, k.fn_name) for k in order[:12]})
    # Non-escalated units rank behind the whole escalated cohort.
    tail = {(k.file, k.fn_name) for k in ranked[-len(_NOT_ESCALATED):]}
    assert tail == _NOT_ESCALATED


def test_the_service_buckets_order_the_rest_and_a_stub_is_never_served():
    """Never served, served, served and graded flat, red-dead, dead. A stub's
    stale service stamp is about a body no longer on disk (run
    `3453127-045b05b1`, `vtable`)."""
    keys = ["dead", "red", "flat", "served", "fresh", "stub"]
    served = {"dead", "red", "flat", "served", "stub"}
    svc = {k: service_of(k, served=served, flat={"flat"}, dead={"dead"},
                         red_dead={"red"}, hollow={"stub"}) for k in keys}
    assert svc == {"dead": Service.DEAD, "red": Service.RED_DEAD,
                   "flat": Service.FLAT, "served": Service.SERVED,
                   "fresh": Service.NEVER, "stub": Service.NEVER}
    facts = {k: UnitFacts(static_index=i, service=svc[k])
             for i, k in enumerate(keys)}
    assert rank(facts) == ["fresh", "stub", "served", "flat", "red", "dead"]


def _recorded_chain_1577437() -> list[FnLocator]:
    path = (Path(__file__).resolve().parent / "fixtures" / "crash"
            / "chain_blame_1577437-c93d5c26_2026-09-06_iter3.json")
    data = json.loads(path.read_text(encoding="utf-8"))["event"]["data"]
    assert data["provenance"] == "chain_derived" and data["chain_count"] == 63
    return [FnLocator(*n.split("::", 1)) for n in data["blamed"]]


def _pre_fix_cursor(ordered: list[FnLocator], attempted: set[FnLocator],
                    cap: int, exempt: set[FnLocator]) -> list[FnLocator]:
    """The deleted cursor's cut: what the pass already offered, minus the
    exempt keys, was subtracted before the cap."""
    seen = attempted - exempt
    return [k for k in ordered if k not in seen][:cap]


def test_recorded_1577437_chain_cohort_is_offered_once_each_before_any_twice():
    """On the recorded cohort under the live cap of 12, every chain unit is
    offered before any is offered twice, and the pass completes on the fifth
    sweep."""
    chain = [f"{k.file}::{k.fn_name}" for k in _recorded_chain_1577437()]
    assert len(chain) == 50

    def facts(k: str, i: int) -> UnitFacts:
        return UnitFacts(static_index=i, crash=Crash.CHAIN)

    batches = _sweeps([chain] * 5, DEFAULT_MAX_DISPATCH, facts=facts)
    offered = [k for b in batches for k in b.keys]
    assert len(set(offered[:48])) == 48 and set(offered[:50]) == set(chain)
    assert [b.attempted_before for b in batches] == [0, 12, 24, 36, 48]
    assert batches[-1].completes_pass and not batches[0].completes_pass


def test_negative_control_the_old_exemption_re_buys_the_cohort_head_forever():
    """The recorded behaviour: every crash key exempt from the cursor. The
    same 12 come back on every batch and the 38 below the head are never
    offered."""
    chain = _recorded_chain_1577437()
    attempted: set[FnLocator] = set()
    heads = []
    for _ in range(6):
        head = _pre_fix_cursor(chain, attempted, 12, exempt=set(chain))
        heads.append(tuple(head))
        attempted |= set(head)
    assert all(h == heads[0] for h in heads)


# --- why a unit is not in the batch ------------------------------------------

def test_a_held_unit_may_only_be_withheld_as_cfg_inactive():
    w: Withheld[str] = Withheld()
    w.note(["h1"], "standing_flat")
    w.note(["h2"], "cfg_inactive")
    w.note(["u"], "exhausted")
    w.note(["h1"], "crash_scoped")   # the first reason stands
    assert w.held_violations({"h1", "h2"}) == {"h1": "standing_flat"}


def test_off_batch_reasons_name_the_filter_or_the_rank_position():
    """The line that said `absent from this sweep's frontier` named a cause
    that could not be true; the reason is computed now."""
    ranked = [f"u{i}" for i in range(20)]
    w: Withheld[str] = Withheld()
    w.note(["gone"], "exhausted")
    out = off_batch_reasons(["u0", "u15", "gone", "nowhere"], ranked,
                            ranked[:12], w, 12)
    assert out == {"u15": "rank 16 past the cap of 12",
                   "gone": "withheld: exhausted",
                   "nowhere": "not on the frontier"}


def test_the_classes():
    assert UnitFacts(static_index=0, held_tests=2).rank_class is RankClass.HELD
    assert UnitFacts(static_index=0, owed_tier=1).rank_class is RankClass.EVIDENCED
    assert (UnitFacts(static_index=0, crash=Crash.SITED).rank_class
            is RankClass.EVIDENCED)
    assert (UnitFacts(static_index=0, crash=Crash.CHAIN).rank_class
            is RankClass.OTHER)
    assert UnitFacts(static_index=0).rank_class is RankClass.OTHER
