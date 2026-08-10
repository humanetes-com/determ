"""leaf_first_group_order: fewest cross-group symbol overlaps first, name tiebreak."""
from __future__ import annotations

from pipeline_core.application.reconstruct.scheduling import leaf_first_group_order


def test_leaf_with_no_shared_symbols_comes_first() -> None:
    groups = [
        {"name": "hub", "target_symbols": ["a", "b"]},
        {"name": "leaf", "target_symbols": ["z"]},      # shares nothing
        {"name": "dep", "target_symbols": ["a"]},        # shares "a" with hub
    ]
    order = [g["name"] for g in leaf_first_group_order(groups)]
    assert order[0] == "leaf"          # 0 overlaps
    assert order.index("dep") < order.index("hub") or order[-1] == "hub"


def test_empty_symbols_count_as_zero_overlap() -> None:
    groups = [{"name": "b", "target_symbols": []}, {"name": "a", "target_symbols": []}]
    # both 0 overlap → name tiebreak
    assert [g["name"] for g in leaf_first_group_order(groups)] == ["a", "b"]


def test_missing_key_is_safe() -> None:
    groups = [{"name": "x"}, {"name": "y", "target_symbols": ["q"]}]
    out = leaf_first_group_order(groups)
    assert {g["name"] for g in out} == {"x", "y"}


def test_unsorted_refill_tail_shatters_levels_and_the_reorder_restores_them() -> None:
    """`group_priority_levels` opens a new barriered level at every ref-count
    change, so it is only as good as its input's contiguity. The engine's
    behavioral re-fill tail arrived in source-scan order and shattered into
    near-singleton levels — measured as a 5.0 h per-fn phase at mean
    concurrency 1.09 against parallelism 4 (run 1159463-b1e352e5 iter 3,
    2026-08-09, a 33-unit all-re-fill batch). The leaf-first reorder is what
    makes equal-count records contiguous again."""
    from pipeline_core.application.reconstruct.scheduling import (
        group_priority_levels,
        leaf_first_fn_order,
        match_records_to_order,
    )
    from pipeline_core.domain.models import Stub

    counts = {"a": 9, "b": 9, "c": 5, "d": 5, "e": 5, "f": 0, "g": 0}
    # Source-scan order interleaves the ref counts (the pre-fix shape).
    scan = [Stub(file="src/x.rs", fn_name=n)
            for n in ["a", "c", "f", "b", "d", "g", "e"]]
    shattered = group_priority_levels(scan, counts)
    assert len(shattered) == 7  # every adjacent pair differs → all singletons

    ordered = match_records_to_order(
        leaf_first_fn_order([s.key for s in scan], counts), scan)
    levels = group_priority_levels(ordered, counts)
    assert [[s.fn_name for _i, s in lvl] for lvl in levels] == [
        ["a", "b"], ["c", "d", "e"], ["f", "g"]]


def test_match_records_to_order_keeps_same_key_records_in_relative_order() -> None:
    from pipeline_core.application.reconstruct.scheduling import (
        match_records_to_order,
    )
    from pipeline_core.domain.models import Stub

    first = Stub(file="src/x.rs", fn_name="fmt", container="impl A")
    second = Stub(file="src/x.rs", fn_name="fmt", container="impl B")
    other = Stub(file="src/y.rs", fn_name="new")
    out = match_records_to_order(
        [other.key, first.key, first.key], [first, second, other])
    assert out == [other, first, second]
