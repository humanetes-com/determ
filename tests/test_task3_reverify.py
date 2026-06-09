"""Regression: TASK 3 re-verify must terminate — CODE_ARCHITECTURE.md R10.2.

The live microcrate run (session 58) crashed with RecursionError: task3 re-invoked
itself whenever the inline retry merely CHANGED the cargo text. cargo output isn't
byte-deterministic (test ordering/timing), so a no-op retry "changed" it → infinite
recursion. `_should_reverify` now gates on stable test COUNTS + a budget. These pin
both: the bug case (changed-but-not-improved → no re-verify) and real progress.
"""
from __future__ import annotations

from pipeline_core.application.tasks.task3 import _should_reverify

_2P1F = "test result: FAILED. 2 passed; 1 failed"
_2P1F_REORDERED = "test result: FAILED. 2 passed; 1 failed\n(different timing text)"
_3P0F = "test result: ok. 3 passed; 0 failed"
_1P1F = "test result: FAILED. 1 passed; 1 failed"


def test_no_progress_does_not_reverify() -> None:
    """THE BUG: same counts, different text → must NOT recurse."""
    assert _should_reverify(_2P1F, _2P1F_REORDERED, retry_passed=False,
                            budget_remaining=2) is False


def test_fewer_failures_reverifies() -> None:
    assert _should_reverify(_2P1F, _3P0F, retry_passed=False, budget_remaining=2) is True


def test_more_passes_reverifies() -> None:
    """Build-fix progress: tree now compiles so more tests run/pass."""
    assert _should_reverify(_1P1F, _2P1F, retry_passed=False, budget_remaining=2) is True


def test_retry_passed_reverifies() -> None:
    assert _should_reverify(_2P1F, _3P0F, retry_passed=True, budget_remaining=2) is True


def test_exhausted_budget_never_reverifies() -> None:
    """Hard backstop: even genuine progress stops once the budget is spent →
    termination guaranteed (no RecursionError)."""
    assert _should_reverify(_2P1F, _3P0F, retry_passed=True, budget_remaining=0) is False
    assert _should_reverify(_1P1F, _2P1F, retry_passed=False, budget_remaining=0) is False
