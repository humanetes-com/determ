"""Unit tests for the unified loop engine (pipeline_core.application.fix_loop.
run_fix_loop) — the single policy both the outcome view (drive_fix_loop) and the
metric view (run_refine_loop) ride after the P3 unification.

The engine is pure: it reads a StallGauge's `stalls`/`repeated` and dispatches on
the per-attempt StepStatus. A scripted gauge + scripted statuses drive it, no
model/cargo. This pins the exit arithmetic (accept / dead-end / stall / repeated /
hard-cap / initial-done) the two facades depend on."""

from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.application.fix_loop import LoopOutcome, StepStatus, run_fix_loop


@dataclass
class _Gauge:
    """Minimal StallGauge whose stalls/repeated are scripted per attempt."""

    stalls: int = 0
    repeated: bool = False


def _run(statuses, gauge, **kw):
    seen: list[int] = []

    def attempt(n: int) -> StepStatus:
        seen.append(n)
        return statuses[n - 1]

    return run_fix_loop(attempt, gauge, **kw), seen


def test_initial_done_runs_zero_attempts() -> None:
    out, seen = _run([], _Gauge(), hard_cap=5, max_stalls=3, initial_done=True)
    assert out == LoopOutcome(0, "done")
    assert seen == []


def test_accepted_returns_immediately() -> None:
    out, seen = _run([StepStatus.ACCEPTED], _Gauge(), hard_cap=5, max_stalls=3)
    assert out == LoopOutcome(1, "accepted")
    assert seen == [1]


def test_dead_end_is_immediate() -> None:
    out, _ = _run([StepStatus.DEAD_END], _Gauge(), hard_cap=5, max_stalls=3)
    assert out == LoopOutcome(1, "dead_end")


def test_stall_checked_after_attempt_in_order() -> None:
    # Gauge reports stalls==max right after the 2nd CONTINUE → exit at n=2.
    g = _Gauge()
    calls = {"n": 0}

    def attempt(n: int) -> StepStatus:
        calls["n"] = n
        g.stalls = n  # 1, then 2
        return StepStatus.CONTINUE

    out = run_fix_loop(attempt, g, hard_cap=9, max_stalls=2)
    assert out == LoopOutcome(2, "stalled")


def test_repeated_checked_after_stall() -> None:
    # stalls stays under budget; repeated fires on attempt 2 → exit "repeated".
    g = _Gauge()

    def attempt(n: int) -> StepStatus:
        g.repeated = n >= 2
        return StepStatus.CONTINUE

    out = run_fix_loop(attempt, g, hard_cap=9, max_stalls=9)
    assert out == LoopOutcome(2, "repeated")


def test_stall_wins_over_repeated_when_both() -> None:
    # Order matters: stalled is checked before repeated (legacy per-fn order).
    g = _Gauge()

    def attempt(n: int) -> StepStatus:
        g.stalls = 2
        g.repeated = True
        return StepStatus.CONTINUE

    out = run_fix_loop(attempt, g, hard_cap=9, max_stalls=2)
    assert out == LoopOutcome(1, "stalled")


def test_hard_cap_backstop() -> None:
    out, seen = _run([StepStatus.CONTINUE] * 3, _Gauge(), hard_cap=3, max_stalls=9)
    assert out == LoopOutcome(3, "hard_cap")
    assert seen == [1, 2, 3]


def test_before_attempt_runs_once_per_attempt() -> None:
    g = _Gauge()
    hooks: list[int] = []
    statuses = [StepStatus.CONTINUE, StepStatus.ACCEPTED]
    run_fix_loop(lambda n: statuses[n - 1], g, hard_cap=5, max_stalls=9,
                 before_attempt=hooks.append)
    assert hooks == [1, 2]
