"""The one refine-loop driver: generate → measure → (retro on stall) → retry until the strategy
reports done or the stall budget is spent. Centralises the shape that ~5 engine loops each rolled
by hand (task0c augmentation, per-fn fill, the TDD driver, post-merge fill, the compile-fix loop),
so they share one termination policy AND so meta-awareness (`LoopState.context()`) is delivered to
every loop uniformly instead of bolted onto one task.

Application layer: pure orchestration over a :class:`pipeline_core.ports.RefineStrategy`. All I/O
(model, cargo) lives in the strategy; this driver and `LoopState` stay side-effect-free, so the
loop policy is unit-testable with a scripted fake strategy.
"""

from __future__ import annotations

from dataclasses import replace

from ..domain.refine import LoopState
from ..ports import RefineStrategy


def run_refine_loop(
    strategy: RefineStrategy,
    *,
    max_stalls: int = 3,
    hard_cap: int = 10_000,
) -> LoopState:
    """Drive `strategy` until `is_done`, `max_stalls` consecutive non-improving attempts, or the
    `hard_cap` backstop (runaway guard — the real terminator is the stall budget).

    Flow: measure once up front (maybe already done). Then, while not done / under budget / not
    stalled out: on a stall produce a retrospective and stash it as `pending_retro` (so the next
    attempt's `state.context()` shows it), run ONE attempt (generate+apply+measure), and record
    the outcome. The driver assigns `iteration` and recomputes `improved` against the running best
    so a strategy can't mis-report progress.
    """
    state = LoopState()
    metric = strategy.measure()
    state.best_metric = metric
    while (
        not strategy.is_done(metric)
        and len(state.attempts) < hard_cap
        and state.stalls < max_stalls
    ):
        state.pending_retro = strategy.retrospect(state) if state.stalls >= 1 else ""
        outcome = strategy.attempt(state)   # reads state.context(); generate+apply+measure
        improved = outcome.metric > state.best_metric
        state.record(replace(
            outcome,
            iteration=state.iteration,
            improved=improved,
            retro=outcome.retro or state.pending_retro,
        ))
        metric = outcome.metric
    return state
