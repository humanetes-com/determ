"""Unit tests for the refine-loop primitive (CODE_ARCHITECTURE.md §8 / the loop-unification work).

The loop POLICY (stall detection, retro-on-stall, budget, meta-awareness rendering) is pure and
deterministic — driven by a scripted fake strategy, no model or cargo. This is the net the engine
loops are migrated against.
"""

from __future__ import annotations

from pipeline_core.application.refine_loop import run_refine_loop
from pipeline_core.domain.refine import Attempt, LoopState


class _Scripted:
    """A RefineStrategy whose attempts return a scripted metric sequence."""

    def __init__(self, attempt_metrics: list[float], *, target: float) -> None:
        self._metrics = list(attempt_metrics)
        self._target = target
        self._idx = 0
        self.contexts: list[str] = []   # state.context() each attempt saw
        self.retro_count = 0

    def measure(self) -> float:
        return 0.0  # nothing covered before the first attempt

    def is_done(self, metric: float) -> bool:
        return metric >= self._target

    def retrospect(self, state: LoopState) -> str:
        self.retro_count += 1
        return f"retro@{state.iteration}"

    def attempt(self, state: LoopState) -> Attempt:
        self.contexts.append(state.context())
        m = self._metrics[self._idx]
        self._idx += 1
        return Attempt(metric=m)


def test_improves_until_done() -> None:
    s = _Scripted([50, 100], target=100)
    state = run_refine_loop(s, max_stalls=3)
    assert [a.metric for a in state.attempts] == [50, 100]
    assert [a.improved for a in state.attempts] == [True, True]
    assert state.best_metric == 100 and state.stalls == 0
    assert s.retro_count == 0   # never stalled → no retrospectives


def test_stalls_out_after_budget() -> None:
    # improve once, then flatline: stop after max_stalls consecutive non-improving attempts.
    s = _Scripted([50, 50, 50, 50], target=100)
    state = run_refine_loop(s, max_stalls=2)
    assert len(state.attempts) == 3                       # 1 improving + 2 stalls, then stop
    assert [a.improved for a in state.attempts] == [True, False, False]
    assert state.stalls == 2
    assert s.retro_count == 1                             # retro fires once (before the 3rd attempt)


def test_meta_awareness_context() -> None:
    s = _Scripted([50, 50, 50], target=100)
    run_refine_loop(s, max_stalls=2)
    assert s.contexts[0] == ""                            # first attempt: no history
    # third attempt sees iteration, stall count, the trajectory, and the latest retrospective.
    ctx = s.contexts[2]
    assert "attempt 3" in ctx
    assert "consecutive" in ctx
    assert "#1" in ctx and "#2" in ctx                    # trajectory of prior attempts
    assert "retro@3" in ctx                               # pending retrospective surfaced


def test_already_done_runs_nothing() -> None:
    class _Done(_Scripted):
        def measure(self) -> float:
            return 100.0
    s = _Done([], target=100)
    state = run_refine_loop(s)
    assert state.attempts == []                           # done before any attempt


def test_driver_assigns_iteration_and_recomputes_improved() -> None:
    # Strategy returns improved=True wrongly; the driver overrides it from the running best.
    class _Liar(_Scripted):
        def attempt(self, state: LoopState) -> Attempt:
            self.contexts.append(state.context())
            m = self._metrics[self._idx]
            self._idx += 1
            return Attempt(metric=m, improved=True, iteration=999)   # lies
    s = _Liar([10, 5], target=100)
    state = run_refine_loop(s, max_stalls=1)
    assert [a.iteration for a in state.attempts] == [1, 2]           # driver-assigned, not 999
    assert [a.improved for a in state.attempts] == [True, False]     # 5 < 10 → not improved
