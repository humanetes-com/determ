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


def test_crash_overlay_keeps_blamed_frames_at_the_head_of_the_pool() -> None:
    """Histogram ranks alone would put construct (27) ahead of a crash-blamed
    unit that held fewer consumers. Overlay restores crash-outranks as queue
    order of the single pool, not as a separate barriered level."""
    construct = Stub(file="src/error.rs", fn_name="construct")
    blamed = Stub(file="src/error.rs", fn_name="object_drop")
    fmt = Stub(file="src/wrapper.rs", fn_name="fmt")
    hist = {
        construct.key: 27,
        fmt.key: 14,
        blamed.key: 3,
    }
    from pipeline_core.domain.dispatch_batch import overlay_crash_on_ranks
    ranks = overlay_crash_on_ranks(hist, {blamed.key: 0})
    ordered = rank_stubs_by_blocker_counts([fmt, construct, blamed], ranks)
    levels = group_priority_levels(ordered, None, unit_ranks=ranks)
    assert len(levels) == 1
    names = [s.fn_name for _i, s in levels[0]]
    assert names[0] == "object_drop"
    assert names.index("construct") < names.index("fmt")
