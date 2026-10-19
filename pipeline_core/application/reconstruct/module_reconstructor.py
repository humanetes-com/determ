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
        reenter: Callable[[Path, str, int], bool] | None = None,
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
        # Re-entry into the per-cluster reconstruction: after EVERY failed
        # iteration, `reenter(project_dir, module, artifacts)` is told how
        # many artifacts the retro admitted (lessons for red tests, pinning
        # tests for blocking bodies) and either clears the task2a/b/c
        # checkpoints so the next iteration's TASK 2 re-enters the deep
        # per-fn/per-impl engines (fills persist in .skeleton-filled/), or
        # returns False: nothing new for the writer, and the module ends red
        # here with its verdict. It used to fire only every
        # TASK2A_STALL_REFILL_ITERS non-improving iterations, with the shallow
        # whole-file fix loop as the only writer in between, and never on the
        # end gate's failure, which exited the process instead (run
        # `1680589-7a6824f8`, 2026-10-19). None = never re-enter.
        self._reenter = reenter

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

            # A stopping rule about red TESTS. An end-gate failure is not one:
            # its suite passed, and its output still lists the failures the
            # original shares (nightly `ui` on run `1680589-7a6824f8`), which
            # read as "nothing repairable" and would end the module before the
            # retro that can pin the blocking body.
            if self._out_of_reach is not None and outcome.end_verdict is None:
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

            if self._reenter is not None:
                if self._reenter(ctx.project_dir, ctx.module_name, result.artifacts):
                    self._tel.log(
                        f"\n  [re-enter] {ctx.module_name}: the retro admitted "
                        f"{result.artifacts} artifact(s) — clearing task2a/b/c so "
                        "the next iteration re-enters per-cluster reconstruction "
                        "(fills persist; the deep engine resumes from the "
                        "remaining frontier and reads what the retro wrote).")
                    self._tel.emit("module.reenter", module=ctx.module_name,
                                   data={"iteration": state.iteration,
                                         "artifacts": result.artifacts,
                                         "failure_type": outcome.failure_type})
                else:
                    why = ("the retro admitted nothing new since the last t2a "
                           "re-entry — another fill would send what it already sent")
                    self._tel.log(f"\n  [re-enter] {ctx.module_name}: {why}; "
                                  "module is non-converged.")
                    self._tel.emit("module.nonconverged", module=ctx.module_name,
                                   data={"iteration": state.iteration,
                                         "reason": "no_new_artifact",
                                         "failure_type": outcome.failure_type})
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
