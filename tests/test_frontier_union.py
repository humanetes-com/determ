"""Characterization for `union_unattributed_stubs` (phase-c frontier fix).

Pins the behaviour that lets a helper stub with NO directly-attributed test
(a leaf called only transitively) still enter the phase-c frontier, instead of
surviving to the post-TASK3 whole-file sweep while its tested callers thrash as
"blocked by an unfilled sibling stub". See CODE_ARCHITECTURE §3 / handover.
"""
from __future__ import annotations

from pipeline_core.domain.attribution import union_unattributed_stubs


def test_unattributed_helper_enters_frontier_with_empty_tests() -> None:
    # `caller` is test-attributed; `classify` is a remaining stub no test names.
    frontier = {("src/sprawl.rs", "caller"): {"test_caller"}}
    remaining = [("src/sprawl.rs", "caller"), ("src/sprawl.rs", "classify")]
    merged = union_unattributed_stubs(frontier, remaining)
    # The un-attributed helper is now a frontier candidate ...
    assert ("src/sprawl.rs", "classify") in merged
    # ... carrying an EMPTY test set so the u2 gate accepts it on compile.
    assert merged[("src/sprawl.rs", "classify")] == set()


def test_attributed_keys_keep_their_test_sets() -> None:
    frontier = {("src/a.rs", "f"): {"t1", "t2"}}
    merged = union_unattributed_stubs(frontier, [("src/a.rs", "f")])
    assert merged[("src/a.rs", "f")] == {"t1", "t2"}


def test_inputs_not_mutated() -> None:
    frontier = {("src/a.rs", "f"): {"t1"}}
    remaining = [("src/a.rs", "g")]
    merged = union_unattributed_stubs(frontier, remaining)
    assert ("src/a.rs", "g") not in frontier          # original untouched
    assert frontier[("src/a.rs", "f")] == {"t1"}
    merged[("src/a.rs", "f")].add("mutated")           # mutating the copy ...
    assert frontier[("src/a.rs", "f")] == {"t1"}       # ... never leaks back


def test_empty_remaining_is_identity() -> None:
    frontier = {("src/a.rs", "f"): {"t1"}}
    merged = union_unattributed_stubs(frontier, [])
    assert merged == frontier
    assert merged is not frontier
