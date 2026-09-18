"""The metric *view* of the one fix-loop driver: generate → measure →
(retro on stall) → retry until the strategy reports done or the stall budget is
spent. Centralises the shape that ~5 engine loops each rolled by hand (task0c
augmentation, per-fn fill, the TDD driver, post-merge fill, the compile-fix
loop), so they share one termination policy AND so meta-awareness
(`LoopState.context()`) is delivered to every loop uniformly.

This is a thin facade over :func:`pipeline_core.application.fix_loop.run_fix_loop`:
the loop policy lives once in the engine; here we adapt a
:class:`pipeline_core.ports.RefineStrategy` to it. `LoopState` is the gauge AND
the result — the driver reads its `stalls`/`repeated` for the exit decision and
the strategy's `attempt` records into it.

Application layer: pure orchestration over the strategy. All I/O (model, cargo)
lives in the strategy; this driver and `LoopState` stay side-effect-free, so the
loop policy is unit-testable with a scripted fake strategy.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any

from ..domain.refine import Attempt, LoopState
from ..log import emit, log
from ..ports import RefineStrategy
from .fix_loop import StepStatus, run_fix_loop


def run_refine_loop(
    strategy: RefineStrategy,
    *,
    max_stalls: int = 3,
    hard_cap: int = 10_000,
) -> LoopState:
    """Drive `strategy` until `is_done`, `max_stalls` consecutive non-improving
    attempts, or the `hard_cap` backstop (runaway guard — the real terminator is
    the stall budget).

    Flow: measure once up front (an already-done strategy runs zero attempts).
    Then each attempt: on a stall stash a retrospective as `pending_retro` (so
    the next attempt's `state.context()` shows it), run ONE attempt
    (generate+apply+measure), record it, and report ACCEPTED to the engine once
    `is_done` — recording first, so the winning attempt is in the history. The
    driver assigns `iteration` and recomputes `improved` against the running best
    so a strategy can't mis-report progress."""
    state = LoopState()
    first = strategy.measure()
    state.best_metric = first

    def attempt(_n: int) -> StepStatus:
        outcome = strategy.attempt(state)  # reads state.context(); generate+apply+measure
        improved = outcome.metric > state.best_metric
        state.record(replace(
            outcome,
            iteration=state.iteration,
            improved=improved,
            retro=outcome.retro or state.pending_retro,
        ))
        return StepStatus.ACCEPTED if strategy.is_done(outcome.metric) else StepStatus.CONTINUE

    def before(_n: int) -> None:
        state.pending_retro = strategy.retrospect(state) if state.stalls >= 1 else ""

    run_fix_loop(
        attempt, state, hard_cap=hard_cap, max_stalls=max_stalls,
        before_attempt=before, initial_done=strategy.is_done(first),
    )
    return state


def run_callback_loop(
    *,
    name: str,
    run_check: Callable[[], tuple[bool, str, int]],
    do_fix: Callable[..., dict[str, Any]],
    do_retro: Callable[[str, list[dict[str, Any]]], str] | None = None,
    max_stalls: int = 3,
    hard_cap: int = 10_000,
    on_iter: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Canonical fix-loop over the callback convention — the relocated
    `_improvement_loop`. Adapts the run_check/do_fix/do_retro callbacks
    onto :func:`run_refine_loop` so outer measure loops share one termination
    policy with task0c augmentation.

    Semantics: improvement = strict decrease of the best metric; exit on
    run_check ok, `{"abort": ...}` from do_fix, the stall budget, or the
    hard-cap backstop. Each attempt is RE-MEASURED immediately (run_check after
    do_fix) — recording the post-fix metric, not a one-iteration-late hint.

    `do_fix(output, history, *, retro)` — `retro` carries `LoopState.context()`
    (trajectory + stall count + latest retro); legacy 2-arg callables are still
    accepted. `do_retro` fires before an attempt whenever the loop is stalled.
    Emits `loop.iter` per attempt; history records carry `metric` (pre-fix) and
    `post_fix_metric` (measured)."""
    try:
        _accepts_retro = "retro" in inspect.signature(do_fix).parameters
    except (TypeError, ValueError):
        _accepts_retro = False

    history: list[dict[str, Any]] = []

    class _CallbackStrategy:
        """RefineStrategy over the callback convention. Metric here is
        lower-is-better; LoopState is higher-is-better, so the negated metric
        flows through the driver."""

        def __init__(self) -> None:
            self.ok = False
            self.aborted = False
            self.output = ""
            self.metric = 0
            self.iter_n = 0

        def _check(self) -> None:
            self.ok, self.output, self.metric = run_check()

        def measure(self) -> float:
            self._check()
            return float(-self.metric)

        def is_done(self, metric: float) -> bool:
            return self.ok or self.aborted

        def retrospect(self, state: LoopState) -> str:
            if do_retro is None:
                return ""
            try:
                return do_retro(self.output, history) or ""
            except Exception as e:
                log(f"  [improvement_loop/{name}] do_retro failed: {e}")
                return ""

        def attempt(self, state: LoopState) -> Attempt:
            fix_guidance = state.context()
            retro = state.pending_retro
            pre_metric = self.metric
            output = self.output
            self.iter_n += 1
            record: dict[str, Any]
            try:
                if _accepts_retro:
                    record = do_fix(output, history, retro=fix_guidance) or {}
                else:
                    record = do_fix(output, history) or {}
            except Exception as e:
                log(f"  [improvement_loop/{name}] do_fix raised: {e}")
                record = {"abort": True, "reason": f"do_fix_exception: {e}"}
            if record.get("abort"):
                self.aborted = True
                new_metric = pre_metric          # no re-measure on abort
            else:
                self._check()                    # post-fix truth
                new_metric = self.metric
            improved = float(-new_metric) > state.best_metric
            ev: dict[str, Any] = {
                "name": name,
                "iter": self.iter_n,
                "metric_prev": pre_metric,
                "metric_new": new_metric,
                "metric_best": int(min(-state.best_metric, float(new_metric))),
                "improved": improved,
                "stalled": 0 if improved else state.stalls + 1,
                "fix_summary": record.get("summary", ""),
                "retro_chars": len(fix_guidance),
            }
            with suppress(Exception):
                emit("loop.iter", data=ev)
            if on_iter is not None:
                try:
                    on_iter(ev | {"output": output, "record": record, "retro": retro})
                except Exception as e:
                    log(f"  [improvement_loop/{name}] on_iter raised: {e}")
            history.append({"iter": self.iter_n, **record, "retro": retro,
                            "metric": pre_metric, "post_fix_metric": new_metric})
            if self.aborted:
                log(f"  [improvement_loop/{name}] aborted at iter "
                    f"{self.iter_n}: {record.get('reason', 'unknown')}")
            return Attempt(metric=float(-new_metric), error=output[:2000], retro=retro)

    strat = _CallbackStrategy()
    state = run_refine_loop(strat, max_stalls=max_stalls, hard_cap=hard_cap)
    if strat.ok:
        return True, strat.output, history
    if not strat.aborted:
        if state.stalls >= max_stalls:
            log(f"  [improvement_loop/{name}] floor stalled at "
                f"{int(-state.best_metric)} for {state.stalls} iter(s) — stopping.")
        else:
            log(f"  [improvement_loop/{name}] hard cap {hard_cap} reached "
                "(should never happen — stall budget should fire first).")
    return False, strat.output, history
