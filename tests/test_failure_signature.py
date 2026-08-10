"""Structural-failure detection off failing-test sets."""
from __future__ import annotations

from pipeline_core.domain.failure_signature import (
    attempt_invariant,
    shared_failure_groups,
    structural_group_note,
)


def test_two_consecutive_identical_sets_are_invariant() -> None:
    s = frozenset({"test_a", "test_b"})
    assert attempt_invariant([s, s]) is True


def test_history_shorter_than_two_is_not() -> None:
    assert attempt_invariant([]) is False
    assert attempt_invariant([frozenset({"a"})]) is False


def test_only_the_last_two_attempts_are_compared() -> None:
    a, b = frozenset({"a"}), frozenset({"b"})
    assert attempt_invariant([a, a, b]) is False
    assert attempt_invariant([a, b, b]) is True


def test_oscillation_is_not_invariance() -> None:
    a, b = frozenset({"a"}), frozenset({"b"})
    assert attempt_invariant([a, b, a]) is False


def test_empty_sets_never_match() -> None:
    # A compile failure reports no failing tests; two of those are not the
    # same failure, they are the absence of a test-tier measurement.
    assert attempt_invariant([frozenset(), frozenset()]) is False


def test_three_units_sharing_a_set_form_a_group() -> None:
    s = frozenset({"test_no_backtrace_fallback"})
    groups = shared_failure_groups({
        "from_a": s, "from_b": s, "from_c": s,
        "other": frozenset({"test_x"})})
    assert groups == {s: frozenset({"from_a", "from_b", "from_c"})}


def test_two_units_are_below_the_default_threshold() -> None:
    s = frozenset({"t"})
    assert shared_failure_groups({"a": s, "b": s}) == {}


def test_empty_failing_sets_join_no_group() -> None:
    assert shared_failure_groups(
        {"a": frozenset(), "b": frozenset(), "c": frozenset()}) == {}


def test_min_units_is_adjustable() -> None:
    s = frozenset({"t"})
    assert shared_failure_groups({"a": s, "b": s}, min_units=2) == {
        s: frozenset({"a", "b"})}


def test_group_note_caps_the_test_list() -> None:
    failing = frozenset(f"test_{i}" for i in range(9))
    note = structural_group_note(failing, frozenset({"u1", "u2", "u3"}))
    assert "3 unit(s)" in note
    assert "(+5 more)" in note
    assert "structural" in note
