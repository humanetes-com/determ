"""Pins for the file-first coupled sub-group partition — the route no longer
skips an over-bound file wholesale; test-welded blocks stay joint."""
from __future__ import annotations

from pipeline_core.domain.fill_groups import BlockGroupInput, coupled_fill_groups


def _b(idx: int, fn_count: int, *tests: str) -> BlockGroupInput:
    return BlockGroupInput(idx=idx, fn_count=fn_count, tests=frozenset(tests))


def test_shared_test_welds_blocks_into_one_group() -> None:
    # The vtable shape: the constructor's block and the free drop-family block
    # are validated by the same tests → one joint group.
    groups = coupled_fill_groups(
        [_b(0, 3, "test_boxed", "test_chain"),
         _b(1, 4, "test_chain", "test_downcast"),
         _b(2, 2, "test_fmt")],
        max_fns=20)
    assert [0, 1] in groups
    assert [2] in groups


def test_transitive_welding() -> None:
    groups = coupled_fill_groups(
        [_b(0, 1, "a"), _b(1, 1, "a", "b"), _b(2, 1, "b")], max_fns=10)
    assert groups == [[0, 1, 2]]


def test_component_within_bound_stays_whole() -> None:
    groups = coupled_fill_groups(
        [_b(0, 10, "t"), _b(1, 10, "t")], max_fns=20)
    assert groups == [[0, 1]]


def test_oversized_component_splits_whole_blocks_by_connectivity() -> None:
    # Chain a—b—c—d, 8 fns each, bound 16: split into two coherent pairs,
    # never a split block.
    groups = coupled_fill_groups(
        [_b(0, 8, "ab"), _b(1, 8, "ab", "bc"),
         _b(2, 8, "bc", "cd"), _b(3, 8, "cd")],
        max_fns=16)
    assert all(len(g) == 2 for g in groups)
    assert sorted(i for g in groups for i in g) == [0, 1, 2, 3]
    inputs = {0: frozenset({"ab"}), 1: frozenset({"ab", "bc"}),
              2: frozenset({"bc", "cd"}), 3: frozenset({"cd"})}
    for a, b in groups:
        # the two blocks of each group share at least one test
        assert inputs[a] & inputs[b]


def test_untested_blocks_never_weld() -> None:
    groups = coupled_fill_groups(
        [_b(0, 2), _b(1, 2), _b(2, 3, "t"), _b(3, 3, "t")], max_fns=20)
    assert [2, 3] in groups
    assert [0] in groups and [1] in groups


def test_single_block_over_bound_emerges_alone() -> None:
    groups = coupled_fill_groups([_b(0, 30, "t"), _b(1, 2, "t")], max_fns=20)
    flat = sorted(i for g in groups for i in g)
    assert flat == [0, 1]
    assert [0] in groups or any(g == [0] for g in groups)


def test_deterministic_ordering_heaviest_component_first() -> None:
    a = coupled_fill_groups(
        [_b(0, 2, "x"), _b(1, 9, "y"), _b(2, 9, "y")], max_fns=20)
    b = coupled_fill_groups(
        [_b(0, 2, "x"), _b(1, 9, "y"), _b(2, 9, "y")], max_fns=20)
    assert a == b
    assert a[0] == [1, 2]  # 18 fns outweighs 2


def test_empty_input() -> None:
    assert coupled_fill_groups([], max_fns=20) == []
