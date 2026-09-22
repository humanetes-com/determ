"""C: blocker-histogram ranking on the live `155810-cbb3b9fc` deferred maps.

The plan's fixture, computed from `logs/events.jsonl` (`phase_c.dispatch.deferred`
and `phase_c.fn.fill.start`), not a hand-made constructor list. Negative control
is the recorded iter-1 start order: `src/context.rs::fmt` first,
`construct_from_adhoc` near last, because the engine re-sorted leaf-first after
`take_batch`.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.reconstruct.scheduling import (
    group_priority_levels,
    rank_stubs_by_blocker_counts,
)
from pipeline_core.domain.dispatch import blocker_histogram
from pipeline_core.domain.dispatch_batch import (
    promote_by_blocker_counts,
    take_batch,
)
from pipeline_core.domain.models import FnLocator, Stub

_FIXTURE = Path(__file__).parent / "fixtures" / "dispatch" / "155810-cbb3b9fc_deferred.json"


def _loc(s: str) -> FnLocator:
    file, fn = s.rsplit("::", 1)
    return FnLocator(file, fn)


def _load() -> dict:
    return json.loads(_FIXTURE.read_text())


def _hist(deferred_raw: dict[str, list[str]]) -> dict[FnLocator, int]:
    deferred = {
        _loc(c): frozenset(_loc(b) for b in blks)
        for c, blks in deferred_raw.items()
    }
    return blocker_histogram(deferred)


def test_iter2_histogram_names_construct_as_the_top_blocker() -> None:
    """The plan's computed fixture: iter 2, `error.rs::construct` holds 27 of
    30 deferred consumers."""
    data = _load()
    iter2 = next(d for d in data["deferred"] if d["iter"] == 2)
    hist = _hist(iter2["deferred"])
    construct = FnLocator("src/error.rs", "construct")
    assert hist[construct] == 27
    assert max(hist.values()) == 27


def test_iter2_batch_opens_on_construct() -> None:
    """After `promote_by_blocker_counts` + `take_batch(12)`, construct is slot 0.
    The pre-fix order_frontier ranking left it outside the first batch."""
    data = _load()
    iter2 = next(d for d in data["deferred"] if d["iter"] == 2)
    hist = _hist(iter2["deferred"])
    # Frontier as a bag of the histogram keys plus a few accessors that
    # order_frontier would have put first (high leaf-name frequency).
    accessors = [
        FnLocator("src/wrapper.rs", "fmt"),
        FnLocator("src/context.rs", "fmt"),
        FnLocator("src/error.rs", "from"),
        FnLocator("src/error.rs", "new"),
        FnLocator("src/kind.rs", "new"),
    ]
    producers = list(hist)
    ordered = [(k, frozenset()) for k in accessors + producers]
    ranked = promote_by_blocker_counts(ordered, hist)
    assert ranked[0][0] == FnLocator("src/error.rs", "construct")
    batch = take_batch([k for k, _ in ranked], set(), 12)
    assert batch.keys[0] == FnLocator("src/error.rs", "construct")


def test_iter1_recorded_start_order_is_the_negative_control() -> None:
    """What the live engine actually dispatched: fmt first (09:52:59),
    construct_from_adhoc tenth of thirteen unique starts (11:45:47)."""
    data = _load()
    starts = [row["unit"] for row in data["iter1_fill_start_first_occurrence"]]
    assert starts[0] == "src/context.rs::fmt"
    adhoc = "src/error.rs::construct_from_adhoc"
    assert adhoc in starts
    assert starts.index(adhoc) > starts.index("src/context.rs::fmt")


def test_iter1_ranking_puts_high_count_ahead_of_fmt_in_the_pool() -> None:
    """The engine gap: leaf-first grouping undoes TDD order. With unit_ranks
    from the iter-1 histogram, `construct_from_adhoc` / `ptr.rs::new` dequeue
    before `fmt`. They share one pool — a 15-vs-14 barrier is not the claim."""
    data = _load()
    iter1 = next(d for d in data["deferred"] if d["iter"] == 1)
    hist = _hist(iter1["deferred"])
    starts = [row["unit"] for row in data["iter1_fill_start_first_occurrence"]]
    stubs = [Stub(file=_loc(u).file, fn_name=_loc(u).fn_name) for u in starts]
    ranked = rank_stubs_by_blocker_counts(stubs, hist)
    levels = group_priority_levels(ranked, None, unit_ranks=hist)
    assert len(levels) == 1
    order = [(s.file, s.fn_name) for _i, s in levels[0]]
    adhoc = ("src/error.rs", "construct_from_adhoc")
    ptr_new = ("src/ptr.rs", "new")
    assert adhoc in order or ptr_new in order
    fmt_slots = [
        order.index(p) for p in (
            ("src/context.rs", "fmt"), ("src/wrapper.rs", "fmt"))
        if p in order]
    assert fmt_slots, order
    high = min(i for i, p in enumerate(order) if p in (adhoc, ptr_new))
    assert high < min(fmt_slots)
    # Negative control: grouping the recorded start order on leaf-name
    # frequency (the pre-fix engine) puts fmt in an early level.
    leaf = {"fmt": 9, "new": 9, "from": 5, "construct_from_adhoc": 1}
    recorded = [Stub(file=_loc(u).file, fn_name=_loc(u).fn_name) for u in starts]
    old_levels = group_priority_levels(recorded, leaf)
    old_first = [s.fn_name for _i, s in old_levels[0]]
    assert "fmt" in old_first
    assert "construct_from_adhoc" not in old_first


def _driver_order(hist: dict[FnLocator, int],
                  grades: dict[FnLocator, int]) -> list[FnLocator]:
    """The driver's two ranking steps the pool must follow: blocker counts,
    then crash evidence moved to the front, stable within a grade."""
    from pipeline_core.domain.dispatch_batch import promote_crash_evidence
    ordered = [(k, None) for k in sorted(hist, key=lambda k: (k.file, k.fn_name))]
    ordered = promote_by_blocker_counts(ordered, hist)
    return [k for k, _ in promote_crash_evidence(ordered, grades)]


def _old_overlay(ranks: dict[FnLocator, int],
                 grades: dict[FnLocator, int]) -> dict[FnLocator, int]:
    """The pre-fix rank map: every crash unit replaced by one value per grade."""
    out = dict(ranks)
    ceiling = max(out.values(), default=0)
    span = max(grades.values())
    for k, g in grades.items():
        out[k] = ceiling + 1 + (span - g)
    return out


def test_pool_starts_the_largest_hold_first_when_every_unit_shares_a_crash_grade() -> None:
    """Every unit frame-sited at one grade, blocker counts inverse to file
    order. The pool's first `parallelism` starts are the premise gate's whole
    fill when the first keep moves the ground, so they must be the driver's
    head — the largest hold — not the alphabetically first files (run
    `311306-c8624d12`, 2026-09-22: 12 units tied at rank 46, the 45-test
    holder started fourth in iter 2 and not at all in iter 1)."""
    from pipeline_core.domain.dispatch_batch import dispatch_ranks_of
    holds = {"src/aa.rs": 3, "src/bb.rs": 5, "src/cc.rs": 7,
             "src/xx.rs": 20, "src/yy.rs": 30, "src/zz.rs": 40}
    stubs = [Stub(file=f, fn_name="unit") for f in holds]
    hist = {s.key: n for s, n in zip(stubs, holds.values(), strict=True)}
    grades = dict.fromkeys(hist, 1)
    order = _driver_order(hist, grades)
    assert [k.file for k in order[:3]] == ["src/zz.rs", "src/yy.rs", "src/xx.rs"]

    ranks = dispatch_ranks_of(order)
    pool = rank_stubs_by_blocker_counts(list(reversed(stubs)), ranks)
    levels = group_priority_levels(pool, None, unit_ranks=ranks)
    assert len(levels) == 1
    assert [s.file for _i, s in levels[0]][:4] == [k.file for k in order[:4]]

    # Negative control: the replaced overlay ties all six and the pool opens
    # on file order, the three smallest holds first.
    old = rank_stubs_by_blocker_counts(list(stubs), _old_overlay(hist, grades))
    assert [s.file for s in old[:3]] == ["src/aa.rs", "src/bb.rs", "src/cc.rs"]


def test_pool_keeps_an_observed_frame_above_a_larger_uncrashed_hold() -> None:
    """Crash-outranks survives: a blamed frame with a small hold still starts
    ahead of an uncrashed unit with a large one, because the driver put it
    there and the pool reads the driver."""
    from pipeline_core.domain.dispatch_batch import dispatch_ranks_of
    big = Stub(file="src/a.rs", fn_name="builder")
    blamed = Stub(file="src/z.rs", fn_name="dropper")
    mid = Stub(file="src/m.rs", fn_name="accessor")
    hist = {big.key: 27, mid.key: 14, blamed.key: 3}
    ranks = dispatch_ranks_of(_driver_order(hist, {blamed.key: 0}))
    names = [s.fn_name for s in rank_stubs_by_blocker_counts([mid, big, blamed], ranks)]
    assert names == ["dropper", "builder", "accessor"]
