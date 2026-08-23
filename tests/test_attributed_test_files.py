"""Slice E: the attributed-test lookup budgets DISTINCT FILES, not leaf names.

The two are different numbers and the gap is not cosmetic. Several attributed
tests routinely live in one file; every call site dedups by path; so a
name-capped walk can spend its whole budget on names that collapse onto the
files it already has, and a file holding the failures the escalated unit
actually owns never reaches the prompt. That is what run `263257-9e46def4`
iter 3 (2026-08-22) did to `tests/test_downcast.rs`.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.test_locator import (
    attributed_test_blocks,
)

# Four leaves, three files: the first two share a file, exactly as
# `test_downcast_high`/`test_downcast_mid` share `tests/test_context.rs`.
_WHERE = {
    "test_downcast_high": "tests/test_context.rs",
    "test_downcast_mid": "tests/test_context.rs",
    "test_boxed_str": "tests/test_boxed.rs",
    "test_downcast": "tests/test_downcast.rs",
}


def _lookup(leaf: str) -> list[tuple[str, str]]:
    path = _WHERE.get(leaf)
    return [(path, f"// source of {path}")] if path else []


def test_collapsing_leaves_do_not_consume_the_file_budget() -> None:
    leaves = ("test_downcast_high", "test_downcast_mid", "test_boxed_str",
              "test_downcast")
    got = [p for p, _ in attributed_test_blocks(leaves, _lookup, file_cap=3)]
    assert got == ["tests/test_context.rs", "tests/test_boxed.rs",
                   "tests/test_downcast.rs"]
    # The negative control, i.e. the pre-fix rule run on the same input: cap
    # the LEAVES, then dedup by path. Two of the first three names are the same
    # file, so the budget buys two files and the third is never looked up.
    old: list[str] = []
    for leaf in leaves[:3]:                      # <- the cap was on names
        for path, _txt in _lookup(leaf):
            if path not in old:
                old.append(path)
    assert old == ["tests/test_context.rs", "tests/test_boxed.rs"]
    assert "tests/test_downcast.rs" not in old   # the file that held the defect


def test_file_cap_is_honoured_and_paths_are_deduped() -> None:
    leaves = tuple(_WHERE)
    got = attributed_test_blocks(leaves, _lookup, file_cap=2)
    assert [p for p, _ in got] == ["tests/test_context.rs", "tests/test_boxed.rs"]
    assert len(got) == 2


def test_seeded_files_count_against_the_budget_and_are_kept() -> None:
    seed = [("tests/test_context.rs", "// already here")]
    got = attributed_test_blocks(("test_boxed_str", "test_downcast"), _lookup,
                                 file_cap=2, into=seed)
    assert [p for p, _ in got] == ["tests/test_context.rs", "tests/test_boxed.rs"]
    assert got[0][1] == "// already here"     # the seed's text is not re-read
    assert seed == [("tests/test_context.rs", "// already here")]   # not mutated


def test_unresolvable_leaf_costs_nothing() -> None:
    got = attributed_test_blocks(("no_such_test", "test_downcast"), _lookup,
                                 file_cap=2)
    assert [p for p, _ in got] == ["tests/test_downcast.rs"]
    assert attributed_test_blocks((), _lookup, file_cap=4) == []
