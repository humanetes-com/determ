"""ModuleReconstructor (P5) — coordinates the TASK 2-4 reconstruction loop.

The thin state machine that was `main()`'s inner `while True`: iterate
ReconstructionStep until a verdict locks the module; on each failure run
FailureAnalysisStep, which advances the immutable IterationState and threads the
running ``total_iters`` / ``coverage_strategy``. No business logic here — only
sequencing the two use-cases over the loop state.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.reconstruction import IterationState
from pipeline_core.ports.reconstruct import Telemetry


class ModuleReconstructor:
    def __init__(
        self, *, step: ReconstructionStep, analysis: FailureAnalysisStep,
        telemetry: Telemetry, plateau_abort: int = 0,
    ) -> None:
        self._step = step
        self._analysis = analysis
        self._tel = telemetry
        # Outer-loop fixed-point terminator (debt #16 follow-up): stop after this
        # many consecutive non-improving iterations. The loop previously only exited
        # on a lock, so a module the model can't converge looped unbounded (the
        # hardcrate `cap_limit` hang). 0 = disabled. Must exceed the whole-project
        # context-escalation threshold so escalation gets to act first.
        self._plateau_abort = plateau_abort

    def run(
        self, ctx: RunContext, *, total_iters: int, coverage_strategy: str,
    ) -> tuple[int, str]:
        """Reconstruct one module to a lock (or single-module completion → exit).
        Returns the updated (total_iters, coverage_strategy) for the next module.
        Returns WITHOUT a lock when the module plateaus (non-converged) — the
        caller's FINAL test pass then reports the real (failing) status instead of
        the loop spinning forever."""
        state = IterationState()
        while True:
            self._tel.log(f"\n{'=' * 50}")
            self._tel.log(f"MODULE {ctx.module_name} — ITERATION {state.iteration}")
            self._tel.log(f"{'=' * 50}")
            # debt#7: structured module/iteration boundary (monitor reads this instead
            # of scraping the "MODULE … — ITERATION …" log line).
            self._tel.emit("module.iter", module=ctx.module_name, iteration=state.iteration)

            outcome = self._step.run(ctx, state, coverage_strategy=coverage_strategy)
            if outcome.locked:
                break

            result = self._analysis.run(
                ctx, state, outcome,
                total_iters=total_iters, coverage_strategy=coverage_strategy)
            state = result.state
            total_iters = result.total_iters
            coverage_strategy = result.coverage_strategy

            if self._plateau_abort and state.stuck(self._plateau_abort):
                self._tel.log(
                    f"\n  [plateau] {ctx.module_name}: no pass-count improvement for "
                    f"{state.no_improvement_streak} consecutive iteration(s) "
                    f"(cap {self._plateau_abort}) — module is non-converged; stopping "
                    "the repair loop (best tree kept). Falling through to the final "
                    "test verdict.")
                self._tel.emit("module.nonconverged", module=ctx.module_name,
                               data={"iterations": state.iteration,
                                     "streak": state.no_improvement_streak,
                                     "best_pass": state.prev_pass_count})
                break
        return total_iters, coverage_strategy
