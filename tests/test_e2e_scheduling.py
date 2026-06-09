"""L1 scheduling-determinism tests — CODE_ARCHITECTURE.md R10.2 (pins R11.3).

`_group_priority_levels` is the deterministic core of phase-c fill batching: it
groups leaf-first-sorted stubs into equal-ref-count levels, and the dispatch loop
puts a barrier between levels so callees fill before callers regardless of worker
scheduling. These pin that grouping so the determinism guarantee can't silently
regress (the bug class R11.3 fixed: free-for-all queue → unreproducible fills).
"""
from __future__ import annotations

import pipeline


def _stub(name: str) -> dict:
    return {"fn_name": name}


def test_equal_refcount_stubs_share_a_level() -> None:
    stubs = [_stub("a"), _stub("b"), _stub("c"), _stub("d")]
    counts = {"a": 3, "b": 3, "c": 1, "d": 1}  # leaf-first already
    levels = pipeline._group_priority_levels(stubs, counts)
    names = [[r["fn_name"] for _i, r in lvl] for lvl in levels]
    assert names == [["a", "b"], ["c", "d"]]  # two ref-count tiers → two levels


def test_indices_are_1_based_and_contiguous() -> None:
    stubs = [_stub("a"), _stub("b"), _stub("c")]
    counts = {"a": 2, "b": 1, "c": 1}
    levels = pipeline._group_priority_levels(stubs, counts)
    flat = [(i, r["fn_name"]) for lvl in levels for i, r in lvl]
    assert flat == [(1, "a"), (2, "b"), (3, "c")]


def test_callees_precede_callers() -> None:
    """Higher ref-count (callee) levels come first → filled before their callers
    under the dispatch barrier."""
    stubs = [_stub("hot"), _stub("mid"), _stub("leaf")]
    counts = {"hot": 9, "mid": 4, "leaf": 0}
    levels = pipeline._group_priority_levels(stubs, counts)
    first_of = [lvl[0][1]["fn_name"] for lvl in levels]
    assert first_of == ["hot", "mid", "leaf"]  # strictly descending priority order


def test_deterministic_and_total() -> None:
    """Same input → same partition, and every stub lands in exactly one level."""
    stubs = [_stub(c) for c in "abcdef"]
    counts = {"a": 5, "b": 5, "c": 5, "d": 2, "e": 2, "f": 0}
    a = pipeline._group_priority_levels(stubs, counts)
    b = pipeline._group_priority_levels(stubs, counts)
    assert a == b
    assert sum(len(lvl) for lvl in a) == len(stubs)


def test_missing_or_empty_counts_one_level() -> None:
    stubs = [_stub("a"), _stub("b")]
    assert len(pipeline._group_priority_levels(stubs, None)) == 1
    assert len(pipeline._group_priority_levels(stubs, {})) == 1
    assert pipeline._group_priority_levels([], {"a": 1}) == []
