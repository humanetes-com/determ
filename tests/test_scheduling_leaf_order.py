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
