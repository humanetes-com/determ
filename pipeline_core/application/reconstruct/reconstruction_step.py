"""ReconstructionStep — ONE TASK 2-4 iteration's generate→test→verdict.

Generates source (TASK 2), reattaches preserved gen_tests, checks API-surface
conformance, runs the strict test verdict (TASK 3), and dispatches the verdict:
a pass (or no-test build-pass, or fix-loop-reverified pass) LOCKS the module and
ends the loop; anything else returns a typed failure for TASK 4. Depends only on
the generation/test/fix roles (ISP); the lock transition and gen_tests reattach
are reused from their own units.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.application import orchestrator
from pipeline_core.application.gen_tests_reattach import ReattachDeps, reattach_gen_tests
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.end_verdict import EndVerdict
from pipeline_core.domain.reconstruction import (
    PROC_MACRO_CONSTRAINTS,
    IterationState,
    plan_whole_project_context,
)
from pipeline_core.domain.test_results import parse_test_counts, resolve_task3_verdict
from pipeline_core.phases import phase_scope
from pipeline_core.ports.reconstruct import (
    Artifacts,
    Fixer,
    Generator,
    Telemetry,
    TestRunner,
)

NO_IMPROVEMENT_THRESHOLD_DEFAULT = 3


#: The failure type of an iteration whose suite passed and whose end gate
#: did not: a body reachable under a baked cfg-set is still unimplemented and
#: no oracle test reaches it (`domain/end_verdict`).
BLOCKING_BODY = "blocking_body"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Result of one reconstruction iteration: either the module locked (loop
    ends) or a typed failure to feed TASK 4. `end_verdict` is set exactly
    when the failure is the end gate's (`failure_type == BLOCKING_BODY`)."""
    locked: bool
    failure_type: str = ""
    test_output: str = ""
    end_verdict: EndVerdict | None = None


class ReconstructionStep:
    def __init__(
        self, *, generator: Generator, tests: TestRunner, fixer: Fixer,
        artifacts: Artifacts, telemetry: Telemetry,
        lock_deps: orchestrator.LockDeps, reattach_deps: ReattachDeps,
        no_improvement_threshold: int = NO_IMPROVEMENT_THRESHOLD_DEFAULT,
    ) -> None:
        self._gen = generator
        self._tests = tests
        self._fixer = fixer
        self._artifacts = artifacts
        self._tel = telemetry
        self._lock_deps = lock_deps
        self._reattach_deps = reattach_deps
        self._threshold = no_improvement_threshold

    def _whole_project_context(self, ctx: RunContext, state: IterationState) -> str:
        plan = plan_whole_project_context(
            module_has_tests=ctx.module_has_tests, module_name=ctx.module_name,
            no_improvement_streak=state.no_improvement_streak, threshold=self._threshold)
        wpc = self._artifacts.format_whole_project_context(ctx.test_files, plan.reason) if plan.include else ""
        if plan.escalating:
            self._tel.log(f"  Escalating to whole-project context "
                          f"({state.no_improvement_streak} iters no improvement).")
        if "proc-macro" in ctx.crate_type.lower():
            wpc += PROC_MACRO_CONSTRAINTS
        return wpc

    def _lock(self, ctx: RunContext, state: IterationState, *, commit_message: str,
              detail: str, set_current_label: str, locked_commit_msg: str,
              success_log: str, tag: str | None = None,
              complete_commit_msg: str | None = None) -> EndVerdict | None:
        return orchestrator.lock_or_complete(
            ctx.project_dir, ctx.module_name, ctx.module_dir, ctx.module_manifest,
            is_multi_module=ctx.is_multi_module, locked_modules=ctx.locked_modules,
            src_hash=ctx.src_hash, commit_message=commit_message, detail=detail,
            set_current_label=set_current_label, locked_commit_msg=locked_commit_msg,
            success_log=success_log, tag=tag, complete_commit_msg=complete_commit_msg,
            deps=self._lock_deps)

    def _locked_or_blocked(self, verdict: EndVerdict | None,
                           test_output: str) -> StepOutcome:
        """The outcome of a lock attempt: locked, or the end gate's failure
        carried as a failed iteration with its verdict as evidence."""
        if verdict is None or verdict.clean:
            return StepOutcome(True)
        self._tel.emit("end_gate.blocked", data={"blocking": list(verdict.labels())})
        return StepOutcome(False, BLOCKING_BODY,
                           test_output + "\n" + verdict.report(),
                           end_verdict=verdict)

    def run(self, ctx: RunContext, state: IterationState, *, coverage_strategy: str) -> StepOutcome:
        it = state.iteration
        locked_dep_context = ""
        if ctx.is_multi_module and ctx.workspace_deps:
            locked_dep_context = self._artifacts.load_locked_source_context(
                ctx.project_dir, ctx.workspace_deps & ctx.locked_modules)

        label: int | str = f"{ctx.module_name}_{it}" if ctx.is_multi_module else it
        whole_project_context = self._whole_project_context(ctx, state)

        # TASK 2: generate source files.
        if not self._gen.task2_inner_loop(
            ctx.project_dir, ctx.genesis_path, ctx.module_req_path, ctx.module_manifest,
            ctx.existing_test_files, ctx.project_profile, ctx.workspace_structure,
            ctx.module_build_check, locked_dep_context=locked_dep_context,
            current_module=ctx.module_name if ctx.is_multi_module else "",
            module_dir_rel=ctx.module_dir_rel, whole_project_context=whole_project_context,
            coverage_strategy=coverage_strategy,
            api_surface=self._artifacts.load_api_surface(ctx.project_dir, ctx.module_name),
            prev_source_context=state.prev_task2_source,
        ):
            self._tel.log("TASK 2 inner loop exhausted — escalating to TASK 4.")
            return StepOutcome(False, "build", "BUILD FAILED: inner loop exhausted all retries.")

        # Anti-corruption: verify expected files are present before running tests.
        expected_paths = self._gen.load_original_structure(ctx.project_dir, ctx.module_name)
        t2_ok, t2_reason = self._gen.validate_task2_output(
            ctx.project_dir, expected_paths, req_path=ctx.module_req_path)
        if not t2_ok:
            self._tel.log(f"  TASK 2 anti-corruption FAILED: {t2_reason} — treating as build failure.")
            return StepOutcome(False, "build", f"BUILD FAILED: {t2_reason}")

        # Reattach preserved gen_tests siblings + decls, then validate.
        reattach_gen_tests(
            ctx.project_dir, ctx.module_name, clusters=ctx.clusters,
            saved_gen_blocks=ctx.saved_gen_blocks,
            existing_test_files=ctx.existing_test_files, deps=self._reattach_deps)

        # API surface conformance: soft check — appended to test_output on failure
        # so TASK 4 sees structural drift alongside cargo's failure.
        api_ok, api_report = self._gen.validate_api_conformance(
            ctx.project_dir, ctx.module_name, ctx.module_dir)
        if not api_ok:
            self._tel.log(f"  ⚠️  {api_report}")

        # TASK 3: run tests.
        self._tel.set_current(ctx.project_dir, f"task3/{ctx.module_name} / running tests (iter {it})")
        with phase_scope("t3", module=ctx.module_name, attempt=it):
            passed, test_output = self._tests.task3(
                ctx.project_dir, ctx.module_test_cmd, label, ctx.project_dir, ctx.genesis_path)
        if not api_ok:
            test_output = test_output + "\n\n--- API surface drift (pre-test) ---\n" + api_report + "\n"
        pass_count, fail_count, _ = parse_test_counts(test_output)

        t3_status, t3_reason = resolve_task3_verdict(
            passed, pass_count, fail_count, ctx.module_has_tests)
        self._tel.log(f"  TASK 3 result: {t3_status} — {t3_reason}")

        if t3_status == "pass":
            verdict = self._lock(
                ctx, state,
                commit_message=f"pipeline: task3 iter {it} — {ctx.module_name} tests pass",
                tag=f"passing-{ctx.module_name}-{it}", detail=f"iteration {it}",
                set_current_label=f"task2-4/{ctx.module_name} / LOCKED iter {it}",
                locked_commit_msg=f"pipeline: locked — {ctx.module_name}",
                success_log=f"\nSUCCESS: all tests passed on iteration {it}.",
                complete_commit_msg=f"pipeline: complete — all tests pass iter {it}")
            return self._locked_or_blocked(verdict, test_output)

        if t3_status == "no_files":
            self._tel.log("  Module has no test files — locking on build success.")
            verdict = self._lock(
                ctx, state,
                commit_message=f"pipeline: build passed (no tests) — {ctx.module_name} iter {it}",
                detail=f"no tests iter {it}",
                set_current_label=f"task2-4/{ctx.module_name} / LOCKED (no tests)",
                locked_commit_msg=f"pipeline: locked (no tests) — {ctx.module_name}",
                success_log="\nSUCCESS: build passed with no module tests.")
            return self._locked_or_blocked(verdict, test_output)

        if t3_status == "no_tests":
            failure_type, test_output = self._fixer.diagnose_no_tests(
                ctx.project_dir, ctx.module_test_cmd, t3_reason, test_output)
            return StepOutcome(False, failure_type, test_output)

        # "fail": targeted fix loop before full regeneration.
        fix_passed, test_output = self._fixer.task2_fix_loop(
            ctx.project_dir, ctx.module_manifest, ctx.existing_test_files, test_output,
            ctx.module_test_cmd, ctx.module_req_path, genesis_path=ctx.genesis_path,
            module_dir_rel=ctx.module_dir_rel)
        if fix_passed:
            # The fix loop's own check passed — but the ONLY authority for SUCCESS
            # is the per-cluster strict runner. Re-run TASK 3 for the verdict.
            with phase_scope("t3", module=ctx.module_name, attempt=it,
                             detail="re-verify after the fix loop"):
                reverified, test_output = self._tests.task3(
                    ctx.project_dir, ctx.module_test_cmd, f"{label}-fixverify",
                    ctx.project_dir, ctx.genesis_path)
            if reverified:
                verdict = self._lock(
                    ctx, state,
                    commit_message=f"pipeline: task3 iter {it} — {ctx.module_name} tests pass (fix loop)",
                    tag=f"passing-{ctx.module_name}-{it}", detail=f"iteration {it} (fix loop)",
                    set_current_label=f"task2-4/{ctx.module_name} / LOCKED iter {it}",
                    locked_commit_msg=f"pipeline: locked — {ctx.module_name}",
                    success_log="\nSUCCESS: all tests passed after fix loop.",
                    complete_commit_msg=f"pipeline: complete — all tests pass iter {it}")
                return self._locked_or_blocked(verdict, test_output)

        return StepOutcome(False, self._fixer.classify_failure(test_output), test_output)
