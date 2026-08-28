"""Blocker-histogram ranks order the per-fn queue; they must not barrier it.

Run `346231-54bc1489` iter 1 (2026-08-28, cluster `stable`): `PIPELINE_PARALLELISM=4`
and the log claimed four concurrent LLM calls, but after file-first the per-fn
phase ran at mean concurrency 0.99 for three hours. The TDD driver ranked the
12-unit batch by the deferral histogram (unique integers 38, 20, 18, 16, 14, …)
and `group_priority_levels(..., unit_ranks=)` opened a new barriered level at
every distinct count — the leaf-first compile-order rule applied to a key that
is not compile-order. The first five units each ran alone; the two count-12
units were the first that could share workers, and they started 12 ms apart.

The plan's claim, pinned on that run's deferred map (not a hand-made constructor
list): the first four of the ranked 12 occupy one pool of width 4. The negative
control is the pre-fix split-on-count-change, which makes those four singleton
levels and reproduces the recorded `phase_c.fn.fill.start` order.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pipeline_core.application.reconstruct.scheduling import (
    group_priority_levels,
    rank_stubs_by_blocker_counts,
)
from pipeline_core.domain.dispatch import blocker_histogram
from pipeline_core.domain.models import FnLocator, Stub

_FIXTURE = Path(__file__).parent / "fixtures" / "dispatch" / "346231-54bc1489_deferred.json"


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


def _pre_fix_split_on_rank_change(
    stubs: list[Stub], ranks: dict[FnLocator, int],
) -> list[list[str]]:
    """The grouper as shipped with histogram `unit_ranks`: a new barriered
    level at every distinct count. Kept here as the negative control — it is
    not a production helper."""
    levels: list[list[str]] = []
    prev: object = object()
    for r in stubs:
        key = ranks.get(r.key, 0)
        if key != prev:
            levels.append([])
            prev = key
        levels[-1].append(f"{r.file}::{r.fn_name}")
    return levels


def _ranked_batch(hist: dict[FnLocator, int], n: int = 12) -> list[Stub]:
    stubs = [Stub(file=k.file, fn_name=k.fn_name) for k in hist]
    return rank_stubs_by_blocker_counts(stubs, hist)[:n]


def test_live_histogram_head_matches_the_recorded_serial_starts() -> None:
    """The fixture is this run, not a list that the grouper is guaranteed to
    pass: the first five `fill.start` events are the five unique-count heads."""
    data = _load()
    hist = _hist(data["deferred"][0]["deferred"])
    recorded = [row["unit"] for row in data["pre_fix_fill_start_first_occurrence"]]
    ranked = _ranked_batch(hist)
    assert [f"{s.file}::{s.fn_name}" for s in ranked[:5]] == recorded[:5]


def test_pre_fix_count_change_barriers_serialise_the_first_four() -> None:
    """Negative control: the old grouper on this histogram gives the pool
    width the live run actually got — 1 — for every unit of the head."""
    data = _load()
    hist = _hist(data["deferred"][0]["deferred"])
    recorded = [row["unit"] for row in data["pre_fix_fill_start_first_occurrence"]]
    ranked = _ranked_batch(hist)
    pre = _pre_fix_split_on_rank_change(ranked, hist)
    assert [len(lvl) for lvl in pre[:5]] == [1, 1, 1, 1, 1]
    assert [lvl[0] for lvl in pre[:5]] == recorded[:5]
    assert min(4, len(pre[0])) == 1
    # The first non-singleton is the count-12 pair, which the live run
    # started in the same millisecond once the five singletons had drained.
    tied = {u for row in data["pre_fix_fill_start_first_occurrence"][5:7]
            for u in [row["unit"]]}
    assert set(pre[5]) == tied
    ts = [row["ts"] for row in data["pre_fix_fill_start_first_occurrence"][5:7]]
    def _parse(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    # Same level, two workers: 12 ms apart on the live record. The five
    # singletons before them were tens of minutes apart.
    assert abs((_parse(ts[0]) - _parse(ts[1])).total_seconds()) < 1.0


def test_ranked_batch_is_one_pool_of_width_four() -> None:
    """Plan: the first four of this 12 occupy one pool, so four workers start
    them together. Ranking is the queue order, not a join between 38 and 20."""
    data = _load()
    hist = _hist(data["deferred"][0]["deferred"])
    recorded = [row["unit"] for row in data["pre_fix_fill_start_first_occurrence"]]
    ranked = _ranked_batch(hist)
    levels = group_priority_levels(ranked, None, unit_ranks=hist)
    assert len(levels) == 1
    assert len(levels[0]) == 12
    assert min(4, len(levels[0])) == 4
    head = [f"{s.file}::{s.fn_name}" for _i, s in levels[0][:4]]
    assert head == recorded[:4]
    assert head == [
        "src/error.rs::construct",
        "src/error.rs::construct_from_boxed",
        "src/ensure.rs::write_str",
        "src/error.rs::context",
    ]
