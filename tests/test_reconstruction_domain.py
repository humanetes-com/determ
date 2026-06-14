"""Reconstruction domain (P5): IterationState transitions + whole-project policy."""
from __future__ import annotations

from pipeline_core.domain.reconstruction import (
    IterationState,
    plan_whole_project_context,
)


def test_advance_on_improvement_resets_streak() -> None:
    s = IterationState(iteration=3, prev_pass_count=5, no_improvement_streak=2)
    nxt = s.advance(current_pass=8, test_output="out", source_context="src")
    assert nxt.iteration == 4
    assert nxt.prev_pass_count == 8
    assert nxt.no_improvement_streak == 0
    assert nxt.prev_test_output == "out"
    assert nxt.prev_task2_source == "src"


def test_advance_without_improvement_increments_streak() -> None:
    s = IterationState(iteration=1, prev_pass_count=5, no_improvement_streak=1)
    assert s.advance(current_pass=5, test_output="", source_context="").no_improvement_streak == 2
    assert s.advance(current_pass=3, test_output="", source_context="").no_improvement_streak == 2


def test_improved_over_prev_and_stuck() -> None:
    s = IterationState(prev_pass_count=4, no_improvement_streak=3)
    assert s.improved_over_prev(5)
    assert not s.improved_over_prev(4)
    assert s.stuck(3)
    assert not s.stuck(4)


def test_state_is_immutable() -> None:
    s = IterationState()
    nxt = s.advance(current_pass=1, test_output="x", source_context="y")
    assert s.iteration == 1 and s.prev_pass_count == 0  # original untouched
    assert nxt is not s


def test_plan_whole_project_zero_tests() -> None:
    p = plan_whole_project_context(
        module_has_tests=False, module_name="m", no_improvement_streak=0, threshold=3)
    assert p.include and not p.escalating and "no tests" in p.reason


def test_plan_whole_project_stall_escalates() -> None:
    p = plan_whole_project_context(
        module_has_tests=True, module_name="m", no_improvement_streak=3, threshold=3)
    assert p.include and p.escalating and "No improvement" in p.reason


def test_plan_whole_project_default_omits() -> None:
    p = plan_whole_project_context(
        module_has_tests=True, module_name="m", no_improvement_streak=1, threshold=3)
    assert not p.include and p.reason == ""
