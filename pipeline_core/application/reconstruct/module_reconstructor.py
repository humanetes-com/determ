"""ModuleReconstructor — coordinates the TASK 2-4 reconstruction loop.

The thin state machine that was `main()`'s inner `while True`: iterate
ReconstructionStep until a verdict locks the module; on each failure run
FailureAnalysisStep, which advances the immutable IterationState and threads the
running ``total_iters`` / ``coverage_strategy``. No business logic here — only
sequencing the two use-cases over the loop state.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.reconstruction import IterationState
from pipeline_core.ports.reconstruct import Telemetry


class ModuleResult(NamedTuple):
    """Outcome of reconstructing one module. converged=False means the plateau
    terminator fired (module is non-converged) — the caller must NOT re-enter the
    module (re-entry resets IterationState, restarting the plateau counter and
    looping the whole module forever)."""
    total_iters: int
    coverage_strategy: str
    converged: bool
    # Set when the loop stopped because nothing left failing is repairable by a
    # body rewrite. Distinct from a plateau: a plateau says the model has not
    # improved for N iterations and might yet, this says no iteration can.
    out_of_reach: str = ""


class ModuleReconstructor:
    def __init__(
        self, *, step: ReconstructionStep, analysis: FailureAnalysisStep,
        telemetry: Telemetry, plateau_abort: int = 0,
        stall_refill_iters: int = 0,
        stall_refill: Callable[[Path, str], bool | None] | None = None,
        out_of_reach: Callable[[RunContext, str], str] | None = None,
    ) -> None:
        self._step = step
        self._analysis = analysis
        self._tel = telemetry
        # Given (ctx, test_output), why no remaining failure can be repaired by
        # rewriting a body — or "" while one still can. Checked BEFORE
        # FailureAnalysisStep, because that step is TASK 4: on `anyhow`
        # 2026-08-06 every TASK 4 call in the run was spent on `ui`, a trybuild
        # harness, and its iteration-1 genesis update was +64 lines of which the
        # new rules 10 and 16 and the `[UI-SNAPSHOT]` model note were all three
        # about that one test — rule 16 instructing the model to treat
        # diagnostic formatting differences as non-failures, which cannot move a
        # verdict decided by cargo's exit code. Those prompts logged
        # `prompt 309,632 chars = 105% of SINGLE_CALL_CHAR_LIMIT`, so past the
        # budget every appended rule displaced context that was doing work.
        self._out_of_reach = out_of_reach
        # Outer-loop fixed-point terminator: stop after this
        # many consecutive non-improving iterations. The loop previously only exited
        # on a lock, so a module the model can't converge looped unbounded (the
        # hardcrate `cap_limit` hang). 0 = disabled. Must exceed the whole-project
        # context-escalation threshold so escalation gets to act first.
        self._plateau_abort = plateau_abort
        # Stalled-iteration re-fill: every `stall_refill_iters` consecutive
        # non-improving iterations, `stall_refill(project_dir, module)` clears
        # the task2a/b/c checkpoints so the NEXT iteration's TASK 2 re-enters
        # the deep per-fn/per-impl reconstruction engines instead of skipping
        # straight to the shallow whole-file fix loop forever (once those
        # markers read "done", TASK 2 does nothing and only the fix loop moves
        # code — observed cycling ~15h on anyhow with zero pass-count change).
        # Fills persist in .skeleton-filled/, so a re-entry resumes from all
        # accumulated work/notes/retros; a re-entry with nothing new plateaus
        # out in a few cheap cargo runs. 0 or None = disabled.
        self._stall_refill_iters = stall_refill_iters
        self._stall_refill = stall_refill

    def run(
        self, ctx: RunContext, *, total_iters: int, coverage_strategy: str,
    ) -> ModuleResult:
        """Reconstruct one module to a lock (or single-module completion → exit).
        Returns converged=False when the module plateaus (non-converged) — the
        caller must exclude the module and fall to the FINAL test verdict instead
        of re-entering it."""
        state = IterationState()
        converged = True
        while True:
            self._tel.log(f"\n{'=' * 50}")
            self._tel.log(f"MODULE {ctx.module_name} — ITERATION {state.iteration}")
            self._tel.log(f"{'=' * 50}")
            # Structured module/iteration boundary (monitor reads this instead
            # of scraping the "MODULE … — ITERATION …" log line).
            self._tel.emit("module.iter", module=ctx.module_name, iteration=state.iteration)

            outcome = self._step.run(ctx, state, coverage_strategy=coverage_strategy)
            if outcome.locked:
                break

            if self._out_of_reach is not None:
                why = self._out_of_reach(ctx, outcome.test_output)
                if why:
                    self._tel.log(
                        f"\n  [out-of-reach] {ctx.module_name}: {why} — no "
                        "iteration can move this verdict, so the repair loop "
                        "stops here instead of running to the plateau cap. "
                        "TASK 4 is not called; the best tree is kept.")
                    self._tel.emit("module.out_of_reach", module=ctx.module_name,
                                   data={"iteration": state.iteration,
                                         "reason": why})
                    return ModuleResult(total_iters, coverage_strategy, False, why)

            result = self._analysis.run(
                ctx, state, outcome,
                total_iters=total_iters, coverage_strategy=coverage_strategy)
            state = result.state
            total_iters = result.total_iters
            coverage_strategy = result.coverage_strategy

            if (self._stall_refill is not None and self._stall_refill_iters
                    and state.no_improvement_streak > 0
                    and state.no_improvement_streak % self._stall_refill_iters == 0):
                self._tel.log(
                    f"\n  [stall-refill] {ctx.module_name}: "
                    f"{state.no_improvement_streak} non-improving iteration(s) — "
                    "clearing task2a/b/c checkpoints so the next iteration "
                    "re-enters per-cluster reconstruction (fills persist; the "
                    "deep engine resumes from the remaining frontier).")
                self._tel.emit("module.stall_refill", module=ctx.module_name,
                               data={"iteration": state.iteration,
                                     "streak": state.no_improvement_streak})
                if self._stall_refill(ctx.project_dir, ctx.module_name) is False:
                    # Nothing new for the writer: t4 admitted no lesson since
                    # the last re-entry, so another t2a would ask what it
                    # already asked. The module ends here, red, with its red
                    # ledger, instead of iterating to the plateau cap.
                    why = ("no new lesson since the last t2a re-entry — "
                           "another fill would send what it already sent")
                    self._tel.log(f"\n  [stall-refill] {ctx.module_name}: {why}; "
                                  "module is non-converged.")
                    self._tel.emit("module.nonconverged", module=ctx.module_name,
                                   data={"iteration": state.iteration,
                                         "reason": "no_new_lesson"})
                    return ModuleResult(total_iters, coverage_strategy, False, why)

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
                converged = False
                break
        return ModuleResult(total_iters, coverage_strategy, converged)
