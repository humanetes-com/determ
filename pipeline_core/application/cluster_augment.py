"""ClusterAugmentor (R2.2) — the TASK 0c compiler-cluster augmentation phase.

The de-monolithed outer `for _cluster in _clusters` loop that used to live inline
in `main()`: for each compiler cluster, augment every module's tests up to the
coverage target (or accept a ceiling / hard-block), then commit the cluster. No
business logic is added or removed here — only the sequencing, lifted behind
narrow role ports (`pipeline_core.ports.augment`) so it is unit-testable with
fakes and `main()` reduces to one `run(ctx)` call.

`AugmentContext` is the immutable input: the static facts the loop reads
(clusters, modules, per-module dir/type/baseline-coverage, config). Per-cluster
mutable bookkeeping (`ModuleAugmentState`, the done-set) stays local to `run`.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.models import BuildCluster, ModuleAugmentState
from pipeline_core.ports.augment import (
    AugmentEngine,
    AugmentStore,
    AugmentTelemetry,
    ClusterScope,
    CommitGate,
)


@dataclass(frozen=True)
class AugmentContext:
    """Immutable input to ClusterAugmentor.run — the facts the cluster loop reads."""

    project_dir: Path
    clusters: Sequence[BuildCluster]
    modules: Sequence[str]
    module_coverage: Mapping[str, float]
    module_dirs: Mapping[str, Path]   # crate_dir per module (project_dir if single)
    module_types: Mapping[str, str]   # crate_type per module ("lib" if single)
    src_hash: str                     # crate identity for pipeline_state.md phase records
    code_id: str                      # pipeline identity (`code_identity`) for give-up sentinels
    project_context: str
    coverage_strategy: str
    target_pct: float
    tarpaulin_bin: Path
    # Percent of the cluster's pinnable harvested behaviours that must be pinned
    # by some generated test before the cluster is committed; 0 = no behaviour
    # rounds (the pin contract still applies to every block, the extra passes
    # do not run).
    behaviour_target_pct: float = 0.0


class ClusterAugmentor:
    # Each behaviour round is one model call per file that still has an
    # unpinned behaviour, plus a compile and a filtered test run per block —
    # on anyhow's 9 files that is a ~10-minute round. Three rounds is the
    # budget for a suite the model does not converge on: a file that pins
    # nothing in three informed shots is a prompt/harvest defect to read in
    # the log, not a fourth round.
    BEHAVIOUR_ROUNDS_MAX = 3

    def __init__(
        self, *, telemetry: AugmentTelemetry, scope: ClusterScope,
        store: AugmentStore, engine: AugmentEngine, gate: CommitGate,
    ) -> None:
        self._tel = telemetry
        self._scope = scope
        self._store = store
        self._engine = engine
        self._gate = gate

    def run(self, ctx: AugmentContext) -> None:
        """Augment every module's tests in every compiler cluster. Pure side
        effects (sentinels + commits); `main()` reads the results back from the
        store on the next phase. A no-op when `ctx.clusters` is empty
        (forward/spec-driven mode)."""
        tel, scope, store, engine, gate = (
            self._tel, self._scope, self._store, self._engine, self._gate)
        project_dir = ctx.project_dir
        src_hash = ctx.src_hash
        code_id = ctx.code_id
        modules = list(ctx.modules)
        clusters = list(ctx.clusters)

        for cluster in clusters:
            scope.set_active(cluster)
            cluster_t0 = time.monotonic()
            tel.log(f"\n{'━' * 60}")
            tel.log(f"  Starting compiler cluster: {cluster.name}")
            tel.emit("compiler.cluster.start", cluster=cluster.name)
            ceiling_pct, ceiling_excl, ceiling_total = engine.estimate_ceiling(
                project_dir, cluster)
            tel.emit("cluster.start", cluster=cluster.name,
                     data={"ceiling_pct": ceiling_pct,
                           "ceiling_excl": ceiling_excl,
                           "ceiling_total": ceiling_total,
                           "active_cfg": list(cluster.active_cfg or []),
                           "excluded_cfg": list(cluster.excluded_cfg or []),
                           "gated_files": sorted(cluster.gated_files or [])})
            if ceiling_excl > 0:
                tel.log(f"  Cluster ceiling estimate: ~{ceiling_pct:.1f}% "
                        f"({ceiling_excl} of {ceiling_total} source lines inside excluded-cfg blocks — "
                        f"unreachable in this cluster)")
            tel.log(f"{'━' * 60}")
            if cluster.gated_files:
                tel.log(f"  [cluster] Skipping {len(cluster.gated_files)} gated file(s) not compiled in this cluster: "
                        f"{', '.join(sorted(cluster.gated_files))}")
            tel.mark_phase(project_dir, f"task0c/{cluster.name}", "in_progress",
                           detail="starting", source_hash=src_hash)
            tel.set_current(project_dir, f"task0c/{cluster.name} / starting")

            module_states: dict[str, ModuleAugmentState] = {}
            cluster_done: set[str] = set()

            # Pre-load which modules are already done for this cluster (resume support).
            is_last_cluster = (cluster == clusters[-1])
            for m in modules:
                # A recorded verdict binds only while it still describes something
                # true. "done" is a claim about the TREE and binds under any code,
                # but only when the stop that wrote it applied the current
                # function-execution criterion (a line-coverage "done" sealed
                # anyhow's `nightly` over functions no test entered); a give-up
                # ("ceiling", "hard_block", "precheck_failed") is a claim about the
                # pipeline, so it is dropped once `code_identity` moves. That is
                # not the crate `source_hash` pipeline_state.md records on phase
                # lines — `phase_valid` does not compare hashes at all, and a
                # pipeline-only extractor fix leaves the crate hash unchanged.
                # Without this, run 2881435-34981ab0 (2026-08-27) sealed anyhow's
                # `stable` cluster at 83.9% on a false ceiling and 1566274-2e1d805c
                # skipped it: the only remover of a cluster sentinel is the full
                # --clean workspace wipe.
                verdict = store.cluster_augmented_verdict(project_dir, cluster.name, m)
                if verdict is not None and not verdict.still_binding(code_id):
                    tel.log(
                        f"  cluster/{cluster.name}: '{m}' sentinel records "
                        f"{verdict.coverage:.1f}% under a "
                        f"'{verdict.reason or 'unattributed'}' verdict "
                        f"{verdict.unbinding_reason(code_id)} — "
                        f"re-augmenting instead of inheriting it."
                    )
                    tel.emit("cluster.sentinel.reopened", cluster=cluster.name,
                             data={"module": m, "coverage": verdict.coverage,
                                   "reason": verdict.reason,
                                   "recorded_code_id": verdict.code_id,
                                   "current_code_id": code_id,
                                   "recorded_criterion": verdict.criterion,
                                   "why": verdict.unbinding_reason(code_id)})
                elif verdict is not None:
                    cluster_done.add(m)
                    tel.log(f"  cluster/{cluster.name}: '{m}' already done — skipping.")
                    # Crash-recovery: if this is the last cluster and the main module sentinel
                    # is missing (pipeline crashed after writing cluster sentinel but before
                    # mark_module_augmented), write it now from the cluster sentinel value.
                    if is_last_cluster and not store.is_module_augmented(project_dir, m):
                        cov_val = store.cluster_augmented_value(project_dir, cluster.name, m)
                        store.mark_module_augmented(project_dir, m, cov_val)
                        tel.log(f"  cluster/{cluster.name}: recovery — wrote main sentinel for '{m}' ({cov_val:.1f}%)")

            # Pre-check: verify base library compiles under this cluster's env.
            # Handles E0554 (nightly feature on stable) by auto-installing nightly toolchain.
            precheck_ok = True
            if cluster_done != set(modules):
                pc_mod = next((m for m in modules if m not in cluster_done), modules[0])
                precheck_ok = engine.pre_check(project_dir, pc_mod, cluster)
                if not precheck_ok:
                    tel.log(f"  cluster/{cluster.name}: pre-check failed — skipping all modules.")
                    tel.mark_phase(project_dir, f"task0c/{cluster.name}", "error",
                                   detail="pre-check failed — cluster skipped", source_hash=src_hash)
                    for m in modules:
                        if m not in cluster_done:
                            store.mark_cluster_augmented(
                                project_dir, cluster.name, m, 0.0,
                                reason="precheck_failed", code_id=code_id)
                            if is_last_cluster:
                                store.mark_module_augmented(project_dir, m, 0.0)
                            cluster_done.add(m)

            while True:
                # Pick next module not yet done for this cluster, highest coverage first.
                pending = [m for m in modules if m not in cluster_done]
                if not pending:
                    break
                mod_name = max(pending, key=lambda m: ctx.module_coverage.get(m, 0.0))
                mod_dir = ctx.module_dirs[mod_name]
                crate_type = ctx.module_types[mod_name]

                if not ctx.tarpaulin_bin.exists():
                    raise RuntimeError(
                        f"HARD STOP: cargo-tarpaulin not found at {ctx.tarpaulin_bin}. "
                        "Install with: cargo install cargo-tarpaulin"
                    )

                tel.log(f"\nTASK 0c [{cluster.name}]: Augmenting '{mod_name}' "
                        f"to {ctx.target_pct:.0f}% coverage...")
                tel.mark_phase(project_dir, f"task0c/{cluster.name}/{mod_name}", "in_progress",
                               detail="starting", source_hash=src_hash)
                tel.set_current(project_dir, f"task0c/{cluster.name}/{mod_name} / starting")
                task0c_t0 = time.monotonic()
                tel.emit("task.start", task="task0c", cluster=cluster.name, module=mod_name)

                etf_pre = engine.collect_test_rels(project_dir)
                cov, covered, at_ceiling, blocked_reason = engine.augment_module(
                    project_dir, mod_name, mod_dir, crate_type,
                    existing_test_files=etf_pre,
                    project_context=ctx.project_context,
                    coverage_strategy=ctx.coverage_strategy,
                )
                tel.log(f"  TASK 0c [{cluster.name}]: '{mod_name}' coverage: {cov:.1f}%")
                tel.emit("task.done", task="task0c", cluster=cluster.name, module=mod_name,
                         duration_ms=int((time.monotonic() - task0c_t0) * 1000),
                         data={"coverage_pct": cov, "covered_lines": covered,
                               "at_ceiling": at_ceiling, "blocked_reason": blocked_reason})
                tel.mark_phase(project_dir, f"task0c/{cluster.name}/{mod_name}",
                               "done" if cov >= ctx.target_pct else "in_progress",
                               detail=f"{cov:.1f}%", source_hash=src_hash)
                tel.set_current(project_dir, f"task0c/{cluster.name}/{mod_name} / {cov:.1f}%")

                state = module_states.setdefault(
                    mod_name, ModuleAugmentState(mod_name, cluster.name,
                                                 target_pct=ctx.target_pct)
                )
                # The function-execution half of the stop, as task0c persisted
                # it this pass. A line target met with functions still
                # unexecuted inside their attempt budget is not done.
                fn_record = store.cluster_fn_record(project_dir, cluster.name, mod_name)
                fns_pending = (len(fn_record.pending)
                               if fn_record is not None and fn_record.measured else 0)
                state.record_pass(cov, covered, at_ceiling, fns_pending=fns_pending)
                status = state.status(crate_type)
                if fn_record is not None:
                    tel.log(f"  TASK 0c [{cluster.name}]: '{mod_name}' {fn_record.summary()}")

                # A blocked pass is deterministic: the harness survived the LLM fix
                # attempts AND a full gen_tests strip (or is broken in protected
                # files), so re-entering augmentation from the identical tree state
                # cannot end differently. Accept and move on instead of burning
                # STALL_LIMIT identical retries (observed live: nightly ×10 no-ops).
                if blocked_reason and status not in ('done', 'ceiling'):
                    tel.log(
                        f"  HARD BLOCK: '{mod_name}' [{cluster.name}] augmentation "
                        f"blocked — {blocked_reason}. Retry would be a deterministic no-op; "
                        f"accepting and moving on."
                    )
                    status = 'hard_block'

                if status in ('done', 'ceiling', 'hard_block'):
                    if status == 'ceiling':
                        tel.log(
                            f"  CEILING [{cluster.name}]: '{mod_name}' at {cov:.1f}% — all remaining "
                            f"uncovered lines are cfg-filtered or LLVM artifacts. No further passes possible "
                            f"in this cluster. Accepting and moving on."
                        )
                    elif status == 'hard_block':
                        tel.log(
                            f"  HARD BLOCK: '{mod_name}' [{cluster.name}] coverage stuck "
                            f"at {cov:.1f}% (stall {state.stall_passes}/{ModuleAugmentState.STALL_LIMIT}, "
                            f"total {state.total_passes}). Accepting and moving on."
                        )
                    store.mark_cluster_augmented(
                        project_dir, cluster.name, mod_name, cov,
                        reason=status, code_id=code_id, functions=fn_record)
                    # Also write the main module sentinel when the last cluster finishes,
                    # so the strip/task1-4 loop sees it as done.
                    if cluster == clusters[-1]:
                        store.mark_module_augmented(project_dir, mod_name, cov)
                    cluster_done.add(mod_name)
                else:
                    _why = (f"coverage {cov:.1f}% < {ctx.target_pct:.0f}%"
                            if cov < ctx.target_pct else
                            f"coverage {cov:.1f}% meets {ctx.target_pct:.0f}% but "
                            f"{fns_pending} function(s) no test executes are still "
                            "inside their attempt budget")
                    tel.log(
                        f"  HARD RULE: '{mod_name}' [{cluster.name}] {_why} "
                        f"(stall {state.stall_passes}/{ModuleAugmentState.STALL_LIMIT}, "
                        f"total {state.total_passes}, best covered {state.best_covered_lines} lines). "
                        f"Retrying."
                    )
                    store.clear_module_sentinel(project_dir, mod_name)
                    continue

            if precheck_ok:
                self._behaviour_rounds(ctx, cluster, [m for m in modules if m in cluster_done])

            tel.log(f"\n  cluster/{cluster.name} complete — {len(cluster_done)} module(s) done.")
            tel.emit("cluster.done", cluster=cluster.name,
                     duration_ms=int((time.monotonic() - cluster_t0) * 1000),
                     data={"modules_done": sorted(cluster_done)})
            cov_summary = ", ".join(
                f"{m}:{store.cluster_augmented_value(project_dir, cluster.name, m):.1f}%"
                for m in cluster_done
                if store.is_cluster_augmented(project_dir, cluster.name, m)
            )
            gate.commit_and_mark(
                project_dir, f"task0c/{cluster.name}", "done",
                commit_message=f"pipeline: task0c/{cluster.name} — {cov_summary}",
                detail=f"{len(cluster_done)} modules",
                source_hash=src_hash,
            )

        scope.set_active(None)

    def _behaviour_rounds(
        self, ctx: AugmentContext, cluster: BuildCluster, modules: Sequence[str],
    ) -> None:
        """After a cluster's coverage loop: raise the share of the cluster's
        harvested behaviours pinned by generated tests up to
        `ctx.behaviour_target_pct`, one bounded pass per round, per module.

        Line coverage is the loop's only stop, and a file at 100 % line
        coverage can still have pinned none of the observables the crate's own
        tests assert (`tests/test_fmt.rs` pins whole `{:#?}` renderings;
        the generated blocks on the 2026-08 anyhow trees asserted `contains`).
        The round reads the metric BEFORE each pass so a cluster already above
        target spends nothing, and stops on the first pass that admits no block
        — a pass that lands nothing cannot move the next reading."""
        target = ctx.behaviour_target_pct
        if target <= 0:
            return
        tel, engine = self._tel, self._engine
        for mod_name in modules:
            mod_dir = ctx.module_dirs[mod_name]
            rounds = 0
            progress = True
            while True:
                pinned, total = engine.behaviour_coverage(
                    ctx.project_dir, cluster, mod_name, mod_dir)
                pct = (pinned / total * 100.0) if total else 100.0
                tel.log(f"  [task0c] cluster {cluster.name}: behaviour coverage {pinned}/{total} "
                        f"({pct:.0f}%) target {target:.0f}% [{mod_name}, round {rounds}]")
                tel.emit("task0c.behaviour_coverage", cluster=cluster.name, module=mod_name,
                         data={"pinned": pinned, "total": total, "pct": pct,
                               "target": target, "round": rounds})
                if total == 0:
                    tel.log(f"  [task0c] cluster {cluster.name}: no pinnable harvested "
                            f"behaviour — no behaviour pass for '{mod_name}'.")
                    break
                if pct >= target:
                    break
                if not progress:
                    tel.log(f"  [task0c] cluster {cluster.name}: behaviour pass admitted no block "
                            f"for '{mod_name}' — stopping below target.")
                    break
                if rounds >= self.BEHAVIOUR_ROUNDS_MAX:
                    tel.log(f"  [task0c] cluster {cluster.name}: {rounds} behaviour round(s) spent "
                            f"on '{mod_name}' — stopping below target.")
                    break
                rounds += 1
                progress = engine.behaviour_pass(
                    ctx.project_dir, cluster, mod_name, mod_dir,
                    project_context=ctx.project_context,
                    coverage_strategy=ctx.coverage_strategy)
