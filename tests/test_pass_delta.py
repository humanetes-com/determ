"""Pins for pass-count comparability (`domain.measurement.compare_measurements`).

The case that motivated it: `pass=14 oracle=69` → `pass=35 oracle=111` was read as
the plateau breaking, when the fill tree's content hash was IDENTICAL across both
readings and every newly-passing name was a test the earlier suite did not have.
"""
from __future__ import annotations

from pipeline_core.domain.measurement import Measurement, compare_measurements


def _m(*, sha: str, passed: set[str], failed: set[str],
       oracle: int | None = None) -> Measurement:
    return Measurement(
        cluster="stable", iteration=1, tree_sha=sha,
        oracle_size=oracle if oracle is not None else len(passed) + len(failed),
        pass_count=len(passed), fail_count=len(failed),
        passed=frozenset(passed), failed=frozenset(failed))


def test_grown_suite_on_unchanged_tree_is_not_comparable():
    prev = _m(sha="aaaa", passed={"t1"}, failed={"t2"})
    cur = _m(sha="aaaa", passed={"t1", "new1"}, failed={"t2", "new2"})
    d = compare_measurements(prev, cur)
    assert not d.comparable
    assert "+2/-0 tests" in d.verdict and "never changed" in d.verdict


def test_grown_suite_with_changed_tree_still_not_comparable():
    """Progress may be real, but it cannot be SEPARATED from the suite change."""
    prev = _m(sha="aaaa", passed={"t1"}, failed={"t2"})
    cur = _m(sha="bbbb", passed={"t1", "new1"}, failed={"t2"})
    d = compare_measurements(prev, cur)
    assert not d.comparable
    assert "cannot be separated" in d.verdict


def test_same_oracle_membership_new_tree_is_real_progress():
    prev = _m(sha="aaaa", passed={"t1"}, failed={"t2", "t3"})
    cur = _m(sha="bbbb", passed={"t1", "t2"}, failed={"t3"})
    d = compare_measurements(prev, cur)
    assert d.comparable
    assert "+1 is real" in d.verdict


def test_same_tree_same_oracle_but_moving_pass_is_flakiness():
    prev = _m(sha="aaaa", passed={"t1"}, failed={"t2"})
    cur = _m(sha="aaaa", passed={"t1", "t2"}, failed=set())
    assert compare_measurements(prev, cur).comparable is False
    assert "flaky" in compare_measurements(prev, cur).verdict


def test_oracle_size_above_name_set_size_is_not_truncation():
    """A test name that runs in several binaries reports a verdict per binary, so
    oracle_size legitimately exceeds the number of distinct names."""
    prev = _m(sha="aaaa", passed={"t1"}, failed={"t2"}, oracle=5)
    cur = _m(sha="bbbb", passed={"t1", "t2"}, failed=set(), oracle=5)
    d = compare_measurements(prev, cur)
    assert d.comparable and "+1 is real" in d.verdict


def test_capped_name_lists_fall_back_to_size_check():
    big = {f"t{i}" for i in range(200)}
    prev = _m(sha="aaaa", passed=big, failed={"z"}, oracle=201)
    cur = _m(sha="bbbb", passed=big | {"z"}, failed=set(), oracle=201)
    d = compare_measurements(prev, cur)
    assert d.comparable and "membership unverified" in d.verdict

    grew = _m(sha="bbbb", passed=big, failed={"z", "y"}, oracle=202)
    assert compare_measurements(prev, grew).comparable is False
