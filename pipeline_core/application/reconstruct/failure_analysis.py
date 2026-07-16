"""FailureAnalysisStep (P5) — the TASK 4 tail of one reconstruction iteration.

Analyse the failure (TASK 4 → genesis update), detect a genesis stall, regen the
coverage docs on a real update, append the failure journal, run the best-tree
ratchet + flow audit + docs telemetry, and produce the next IterationState.
Returns (next_state, total_iters, coverage_strategy) — the loop variables this
step owns. Depends only on the analysis/journal/ratchet/docs roles (ISP).
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.application.reconstruct.reconstruction_step import StepOutcome
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.reconstruction import IterationState
from pipeline_core.ports.reconstruct import (
    Analyst,
    Artifacts,
    CoverageDocs,
    DocsMetrics,
    FailureJournal,
    FlowAudit,
    Ratchet,
    Telemetry,
)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    state: IterationState
    total_iters: int
    coverage_strategy: str


class FailureAnalysisStep:
    def __init__(
        self, *, analyst: Analyst, journal: FailureJournal, ratchet: Ratchet,
        artifacts: Artifacts, coverage_docs: CoverageDocs, audit: FlowAudit,
        docs: DocsMetrics, telemetry: Telemetry, genesis_audit_interval: int,
    ) -> None:
        self._analyst = analyst
        self._journal = journal
        self._ratchet = ratchet
        self._artifacts = artifacts
        self._coverage_docs = coverage_docs
        self._audit = audit
        self._docs = docs
        self._tel = telemetry
        self._genesis_audit_interval = genesis_audit_interval

    def run(
        self, ctx: RunContext, state: IterationState, outcome: StepOutcome, *,
        total_iters: int, coverage_strategy: str,
    ) -> AnalysisResult:
        it = state.iteration
        label: int | str = f"{ctx.module_name}_{it}" if ctx.is_multi_module else it
        failure_type, test_output = outcome.failure_type, outcome.test_output

        self._tel.log(f"  Failure classification: {failure_type}")
        genesis_before = self._artifacts.read(ctx.genesis_path)
        # TASK 4's view of the generated source (requirements-gap vs gen-error).
        t4_source_context = self._artifacts.collect_source_context(
            ctx.project_dir, ctx.module_manifest, ctx.existing_test_files)
        self._tel.set_current(ctx.project_dir, f"task4/{ctx.module_name} / genesis update (iter {it})")
        _t4_ok, current_pass = self._analyst.task4(
            ctx.genesis_path, ctx.module_req_path, test_output, it, ctx.project_dir,
            failure_type=failure_type,
            failure_log=self._journal.load_failure_log(ctx.failure_log_path),
            prev_pass_count=state.prev_pass_count, prev_test_output=state.prev_test_output,
            source_context=t4_source_context,
            api_surface=self._artifacts.load_api_surface(ctx.project_dir, ctx.module_name))
        genesis_after = self._artifacts.read(ctx.genesis_path)

        # Anti-corruption: detect genesis stall (TASK 4 produced no update).
        t4_valid, t4_reason = self._analyst.validate_task4_output(
            genesis_before, genesis_after, test_output, failure_type)
        if not t4_valid:
            self._tel.log(f"  TASK 4 anti-corruption: {t4_reason}")
            t4_summary = f"genesis_stall: {t4_reason}"
            failure_type_logged = "genesis_stall"
        else:
            t4_summary = "genesis.md updated."
            failure_type_logged = failure_type
            # Genesis grew — regenerate coverage_strategy.md so TASK 0c/1 prompts
            # reflect lessons learned this iteration (force=True bypasses the cache).
            self._tel.log("  Regenerating coverage_strategy.md after genesis update...")
            coverage_strategy = self._coverage_docs.regenerate(
                ctx.project_dir, ctx.test_files, ctx.readme_text, ctx.project_profile,
                ctx.clusters)

        self._journal.append_failure_log(
            ctx.failure_log_path, label, failure_type_logged, current_pass,
            state.prev_pass_count, t4_summary)

        improved = state.improved_over_prev(current_pass)
        total_iters += 1

        # debt#12: regression-triggered genesis audit. `state` is still the
        # PRE-update iteration here (next_state.advance happens below), so
        # `state.prev_pass_count` is last iteration's count — a drop below it is a
        # real regression. Audit fires on a regression OR on the fixed interval.
        regression = current_pass < state.prev_pass_count
        if regression or total_iters % self._genesis_audit_interval == 0:
            _why = ("regression "
                    f"({current_pass} < {state.prev_pass_count} prev passes)"
                    if regression
                    else f"every {self._genesis_audit_interval} iters")
            self._tel.log(f"\nGenesis audit triggered ({_why})")
            self._analyst.genesis_audit(ctx.genesis_path)

        # Best-tree ratchet: snapshot on a new-best pass count; restore the best
        # tree if this iteration regressed below it.
        src_rel = [rel for rel in self._artifacts.load_manifest(ctx.module_manifest)
                   if rel not in ctx.existing_test_files]
        best = self._ratchet.best_tree_pass_count(ctx.project_dir)
        if current_pass > (best if best is not None else -1):
            saved_n = self._ratchet.save_best_tree(ctx.project_dir, src_rel, current_pass, iteration=it)
            self._tel.emit("ratchet.save", data={"pass_count": current_pass,
                                                  "files": saved_n, "iter": it})
        elif best is not None and current_pass < best:
            restored = self._ratchet.restore_best_tree(ctx.project_dir)
            self._tel.emit("ratchet.restore", data={"best": best, "current": current_pass,
                                                     "files": len(restored), "iter": it})
        self._tel.log(f"\n  Keeping '{ctx.module_name}' tree for repair iteration "
                      f"{it + 1} (no wipe — docs updated, fix loop continues).")
        self._audit.run(f"iter {it}")

        # Per-iteration doc-richness vs convergence → trendable across runs.
        self._tel.emit("docs.richness", module=ctx.module_name, iteration=it, data={
            "fr_chars": ctx.module_req_path.stat().st_size if ctx.module_req_path.exists() else 0,
            "notes": len(self._docs.model_notes(ctx.module_req_path)),
            "genesis_rules": self._docs.genesis_rules_count(ctx.project_dir, ctx.module_name),
            "pass_count": current_pass, "prev_pass_count": state.prev_pass_count,
            "improved": improved})

        next_state = state.advance(
            current_pass=current_pass, test_output=test_output,
            source_context=t4_source_context)
        return AnalysisResult(state=next_state, total_iters=total_iters,
                              coverage_strategy=coverage_strategy)
