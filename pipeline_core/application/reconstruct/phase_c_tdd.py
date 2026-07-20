"""Phase-C TDD fill driver (P4) — relocated from pipeline.py.

The per-cluster test-driven fill loop: scan stubs → leaf-first frontier → fill via the
PhaseC fn engine → re-run cluster tests → attribute failures → repeat under a FloorStall
budget. Collaborators are injected via `PhaseCTddDeps` (the composition root binds the
concrete pipeline.py helpers, late-bound for the monkeypatch seams); pure domain helpers
(Attempt/LoopState/test parsers) are imported directly. Body relocated verbatim.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.tdd_ratchet import (
    ratchet_breach,
    restore_src_tree,
    snapshot_allowed,
    snapshot_src_tree,
)
from pipeline_core.domain.attribution import union_unattributed_stubs
from pipeline_core.domain.crash_site import crash_evidence_by_locator
from pipeline_core.domain.dispatch import defer_blocked_consumers
from pipeline_core.domain.layout_retrigger import CrashEvidence
from pipeline_core.domain.measurement import Measurement, tree_content_sha
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    Frontier,
)
from pipeline_core.domain.oracle_ledger import SweepOracle, reconcile_sweep
from pipeline_core.domain.refine import Attempt, LoopState
from pipeline_core.domain.test_command import ensure_serial_harness
from pipeline_core.domain.test_report import parse_test_output
from pipeline_core.domain.test_results import (
    parse_binary_results,
    parse_failed_tests,
    parse_test_counts,
    summarize_test_binaries,
)


@dataclass(frozen=True, slots=True)
class PhaseCTddDeps:
    """Collaborators the TDD fill driver needs, injected by the composition root."""

    attribute_failures: Callable[..., Any]
    build_test_reverse_map: Callable[..., Any]
    filter_test_targets: Callable[..., Any]
    check_graceful_stop: Callable[..., Any]
    converge_in_cluster: Callable[..., Any]
    count_relevant_notes: Callable[..., Any]
    effective_stalls: Callable[..., Any]
    emit: Callable[..., Any]
    log: Callable[[str], None]
    order_frontier: Callable[..., Any]
    record_tdd_regression: Callable[..., Any]
    reset_exhaustion_flags: Callable[..., Any]
    run_cargo: Callable[..., Any]
    scan_unimplemented_for_cluster: Callable[..., Any]
    scan_unimplemented_functions: Callable[..., Any]
    task2_fill_fns_one_by_one: Callable[..., ClusterFillResult]
    # Futility escalation: (src_dir, test, futile_units,
    # futile_containers) -> Frontier additions. None disables escalation.
    escalate_to_producers: Callable[..., Frontier] | None = None
    # Persist a new pass high-water fill tree to heal's restore tiers:
    # (project_dir, cluster_name, filled_dir, pass_count, iteration) -> None.
    # None disables checkpointing (heal then has only excision + skeleton).
    checkpoint_fill_tree: Callable[..., None] | None = None
    # F11 M3: (project_dir, module, cluster_name, target_src_dir, error_text)
    # -> RetriggerResult, tried once at the stall-exit before giving up on the
    # cluster. None disables it — the exit behaves exactly as before F11 M3.
    retrigger_layout: Callable[..., Any] | None = None
    # FR-024 at the sweep level: (project_dir, cluster) -> (expected_count,
    # expected_names) — the cfg-aware static test inventory. None disables
    # oracle reconciliation — sweeps then trust whatever cargo reported.
    active_test_inventory: Callable[..., tuple[int, frozenset[str]]] | None = None
    # Crash localizer: (sweep_output, test_cmd, workspace_root, env) ->
    # tuple[CrashSite, ...] — gdb fast path + ASAN fallback. None disables
    # localization (no CRASH_EVIDENCE prompts, no crash-first frontier).
    localize_crashes: Callable[..., tuple[Any, ...]] | None = None
    # One-time retro evidence migration: (project_dir) -> (cleared, retired).
    # Clears exhaustion verdicts + retires guidance recorded under the old
    # crash-confounded whole-run gate. None disables.
    migrate_retro_evidence: Callable[..., tuple[int, int]] | None = None


def _log_attribution_gap(
    deps: PhaseCTddDeps, tag: str, iter_n: int, fail_count: int,
    cur_failed: set[str], frontier_size: int, kind: str,
) -> None:
    """Diagnose a give-up iter where tests still fail but no fn was targeted.

    Disambiguates the ways attribution can silently starve the loop:
      parsed==0                → parse_failed_tests couldn't read the names (parser gap)
      parsed>0 & candidates==0 → names parsed fine but NO attribution path
                                 (behavioral/reverse-map) mapped them to a fn
      parsed>0 & candidates>0  → candidates existed but all exhaustion-filtered (D3)
    Prints the failing names so we can see which fns should have been re-filled.
    """
    if fail_count <= 0:
        return
    names = sorted(cur_failed)
    sample = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
    deps.log(
        f"  {tag} [phase-c/tdd] iter {iter_n}: ATTRIBUTION GAP — "
        f"{fail_count} test(s) fail, parsed={len(names)} name(s), "
        f"candidates(pre-exhaust-filter)={frontier_size} (last kind={kind}). "
        f"failing: {sample or '(none parsed)'}")


def run_phase_c_tdd(
    deps: PhaseCTddDeps,
    *, project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    cluster_tests: list[tuple[str, str]],
    module: str,
    cluster: BuildCluster,
    check_cmd: str,
    cluster_env: dict[str, str],
    leaf_counts: dict[str, int] | None,
    fr_path: Path | None,
    test_cmd_builder: Callable[..., str],
    tag: str,
) -> ClusterFillResult:
    """Plan 4 — TDD-scoped Phase C driver.

    Outer loop: cargo test → frontier → fill frontier → repeat.

    Per D-decisions resolved 2026-05-27:
      D2: order frontier by ascending panic depth, descending caller count
      D3: re-enter an exhausted fn only if other fills landed OR new
          relevant notes accumulated since the last attempt
      D4: cargo build failure routes into a compile-error frontier
      D5: 120s wall-clock timeout per cargo test invocation
      D7: iter-0 frontier surfaces shallowest fn per test naturally
    """
    # Frontier extraction expects the cargo project root (NOT project_root/src):
    # cargo's panic messages emit `src/foo.rs` paths relative to the project
    # root, and the syn extractor records `file` fields the same way. Passing
    # `project_root/src` would produce double-prefixed paths and zero matches.
    src_dir = target_src_dir
    # Filter to direct children of tests/ — only those are cargo --test targets.
    test_stems = sorted({
        Path(rel).stem
        for rel, _ in cluster_tests
        if "tests" in Path(rel).parts and Path(rel).parent.name == "tests"
    })
    if not test_stems:
        deps.log(f"  {tag} [phase-c/tdd] no test stems in cluster; skipping TDD.")
        return ClusterFillResult(
            remaining=len(deps.scan_unimplemented_for_cluster(
                target_src_dir, cluster)))
    # Serial harness: with parallel test threads a crashing binary kills a
    # RANDOM subset of its tests' verdicts each run (whichever threads printed
    # first), making sweep readings and gate baselines non-comparable draws.
    # `--test-threads=1` pins the crash point, so vanished-test sets are
    # deterministic. Flows into every downstream consumer (sweeps, regression
    # gate, scoped u2 runs) via tdd_test_cmd.
    test_cmd = ensure_serial_harness(test_cmd_builder(test_stems))
    test_timeout = int(os.getenv("PHASE_C_TEST_TIMEOUT", "120"))
    # max_iters is legacy — only the 10k backstop in the loop body honors it.
    # The real terminator is floor-stall on pass_count (max_stalls below).
    max_stalls = deps.effective_stalls(int(os.getenv("TDD_MAX_STALLS", "3")),
                                   deps.converge_in_cluster(cluster) or module)

    deps.log(f"  {tag} [phase-c/tdd] driver=tdd, "
         f"max_stalls={max_stalls}, timeout={test_timeout}s, "
         f"stems={len(test_stems)}")

    if deps.migrate_retro_evidence is not None:
        _mig_cleared, _mig_retired = deps.migrate_retro_evidence(project_dir)
        if _mig_cleared or _mig_retired:
            deps.log(f"  {tag} [phase-c/tdd] retro evidence migration: "
                     f"cleared {_mig_cleared} exhaustion record(s), retired "
                     f"{_mig_retired} guidance text(s) written under the "
                     "pre-binary-scoped gate (confounded verdicts).")

    permanently_exhausted: set[FnLocator] = set()
    # Futility escalation state: per failing test, the frontier
    # unit-set of the previous iteration; a test whose set is UNCHANGED while
    # it kept failing has exhausted its attributed fns — escalate once.
    prev_test_units: dict[str, frozenset[FnLocator]] = {}
    escalated_tests: set[str] = set()
    last_notes_count: dict[FnLocator, int] = {}
    last_total_filled = 0
    # Self-contained snapshot for the exhaustion-reset gate below — deliberately
    # NOT reusing `total_filled == last_total_filled` (that pair is compared
    # earlier in THIS SAME iteration, before `total_filled`'s one update at the
    # bottom of the loop, so at this call site both already reflect the END of
    # the PREVIOUS iteration and would always compare equal; a fresh snapshot
    # updated only when we actually reset avoids relying on that ordering).
    _filled_at_last_reset = 0
    # behavioral re-fill targets (frontier fns already filled
    # with a compiling-but-wrong body) are not stubs, so the stub-based
    # exhaustion path never bounds them → an unfixable wrong body would burn one
    # LLM call every iter until the stall budget alone exits. Count per-fn re-fill
    # attempts and mark a fn exhausted once it caps (D3 still re-admits it on new
    # notes / cluster-wide progress — same escape hatch as stubs).
    refill_attempts: dict[FnLocator, int] = {}
    _refill_max = int(os.getenv("TDD_REFILL_MAX_ATTEMPTS", "2"))

    total_filled = 0
    total_attempts = 0
    total_bytes = 0
    errors: list[str] = []

    # FR-024: improvement-driven loop. Track passing-test count across iters;
    # exit only after `max_stalls` consecutive non-improving iters. The 10k
    # backstop in the loop body is a runaway-safety net only.
    # LoopState is the source of truth for the pass_count floor / stall streak
    # AND the carrier of cumulative meta-awareness (the pass trajectory + the
    # causal "this fill broke these tests" notes) fed back into each fill.
    # `stall`/`best_pass` below are kept as synced mirrors so the existing
    # log/emit lines read unchanged.
    state = LoopState()
    stall = 0
    best_pass = -1
    best_fail = None
    # TDD-iter batch ratchet: snapshot the src tree at each pass_count
    # high-water; restore it when a later iter's fills net-regress the count.
    _batch_ratchet = os.getenv("PHASE_C_TDD_BATCH_RATCHET", "1") not in ("", "0", "false")
    _src_root = target_src_dir / "src"
    _bratchet_best = -1
    _bratchet_snap: dict[str, str] | None = None
    # Persist each new pass high-water to heal's durable restore tiers
    # (best-tree snapshot + workspace-git commit). Tracked independently of
    # the in-memory batch ratchet above so the tiers stay populated even when
    # PHASE_C_TDD_BATCH_RATCHET is off — without this, a parse-corruption heal
    # falls straight through to the skeleton nuke (loses every fill in the file).
    _checkpoint_best = -1
    recently_filled: list[FnLocator] = []
    recently_filled_max = int(os.getenv("TDD_RECENTLY_FILLED_MAX", "50"))
    # Hook 3 — causal-regression tracking. `prev_failed` = the failing-test set
    # from the previous iter; `last_newly_filled` = the fns we materially
    # changed last iter. A test that flips pass→fail right after a fill is a
    # regression attributable to that fill.
    prev_failed: set[str] = set()
    last_newly_filled: list[FnLocator] = []
    _regression_notes = os.getenv("TDD_REGRESSION_NOTES", "1") == "1"
    # Build the test reverse map once per cluster — cheap (one extractor
    # invocation, one regex scan of each test file). Used as the LAST
    # attribution fallback when panics/compile errors/classifier all miss.
    try:
        test_reverse_map = deps.build_test_reverse_map(tests_dir_files, src_dir)
    except Exception as _e:
        deps.log(f"  {tag} [phase-c/tdd] reverse-map build failed: {_e}")
        test_reverse_map = {}

    # FR-024 at the sweep level: the statically-expected oracle for this
    # cluster. Vanished tests (expected, but no verdict in the output — their
    # binary crashed or stopped compiling) are counted as FAILING, so the
    # loop can neither call a shrunken suite "done" nor snapshot/checkpoint a
    # tree that silently lost part of its oracle.
    # Scoped to the very stems this loop's `cargo test --test stem…` runs —
    # the sweep's denominator, not the whole-crate FR-024 walk (which also
    # counts src-side/gen_tests tests this command never executes).
    _expected_count, _expected_names = (
        deps.active_test_inventory(project_dir, cluster, frozenset(test_stems))
        if deps.active_test_inventory is not None else (0, frozenset()))
    _bratchet_missing = 0   # oracle missing_count at the ratchet snapshot
    _ckpt_min_missing: int | None = None  # best (lowest) missing seen
    # Cause-C (runtime crash) retrigger: consecutive sweeps whose oracle was
    # short; at the threshold the frozen component the crashes implicate gets
    # re-authored (see the trigger block below the stall accounting).
    _vanish_streak = 0
    _crash_retrigger_after = int(
        os.getenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2"))
    # One-iter cooldown after a retrigger attempt: the next sweep's shrink is
    # expected (reverted stubs crash until re-filled) and must not confirm-now
    # a fresh attempt before a single fill pass has run.
    _retrig_cooldown = False

    def _reconcile(rc_: int, output_: str, pass_n: int, fail_n: int,
                   failed_: set[str]) -> tuple[int, set[str], SweepOracle]:
        if rc_ == -1 or deps.active_test_inventory is None:
            return fail_n, failed_, SweepOracle(frozenset(), 0)
        _ig = parse_test_counts(output_)[2]
        # Per-binary reading supplies what the flat name sets cannot: the
        # NAMES of cluster-ignored tests (so they are not mistaken for
        # crash-vanished) and the trailer-authoritative executed total (so
        # tests sharing a bare name across binaries are not deduped out of
        # the denominator).
        _bins = parse_binary_results(output_)
        oracle = reconcile_sweep(
            _expected_count, _expected_names,
            parse_test_output(output_).passed, failed_, _ig,
            ignored_names=frozenset(n for b in _bins for n in b.ignored),
            executed_count=sum(b.executed for b in _bins))
        if not oracle.intact:
            _why = []
            if oracle.vanished:
                _why.append(
                    f"{len(oracle.vanished)} named test(s) reported no verdict "
                    f"(binary crashed or failed to compile): "
                    f"{sorted(oracle.vanished)[:20]}")
            if oracle.unnamed_shortfall:
                _why.append(
                    f"{oracle.unnamed_shortfall} unnamed test(s) short of the "
                    f"expected total — every expected NAME reported, so the "
                    f"static inventory under-enumerated (not a crash)")
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: ORACLE SHRUNK — "
                f"counting {oracle.missing_count} as failing. "
                + "; ".join(_why))
            deps.emit("phase_c.tdd.oracle_vanished", data={
                "cluster": cluster.name, "iter": iter_n,
                "expected": _expected_count,
                "missing": oracle.missing_count,
                "vanished": sorted(oracle.vanished)[:200]})
            fail_n += oracle.missing_count
            failed_ = set(failed_) | set(oracle.vanished)
        return fail_n, failed_, oracle

    iter_n = 0
    # Hard cap removed — the sole non-success terminator is `stall >=
    # max_stalls` on the pass_count floor. The 10k backstop here mirrors
    # `_improvement_loop`'s safety net and should never fire in practice.
    _tdd_backstop = int(os.getenv("PHASE_C_TDD_BACKSTOP", "10000"))
    while True:
        iter_n += 1
        if iter_n > _tdd_backstop:
            deps.log(f"  {tag} [phase-c/tdd] backstop {_tdd_backstop} reached — "
                 "stopping. This should never happen — stall budget should "
                 "have fired first.")
            break
        deps.check_graceful_stop(f"TDD iter {iter_n}")
        # One fresh, informed retry per iteration boundary for fns/blocks
        # marked fully_exhausted (a DIFFERENT, disk-persisted gate than this
        # loop's own local `permanently_exhausted` — this one lives in the
        # retrospective cache and is checked INSIDE the per-fn fill engine
        # itself, so `permanently_exhausted`'s own re-admission logic below
        # cannot reach it). Comment-preservation already keeps the failed
        # attempt + cargo error visible in source
        # (fill_one_fn.py's make_failed_attempt_body), and normal per-fn
        # retrospective accounting already re-exhausts on no improvement —
        # this just re-admits them so that machinery gets to run again.
        # Gated on real progress since the LAST reset (not blind per-iter),
        # so a crate that's genuinely plateaued doesn't keep paying for
        # retries with no new context — same bound the debt-46 refill-cap
        # test below this loop protects for a different exhaustion path.
        if iter_n > 1 and total_filled > _filled_at_last_reset:
            _cleared = deps.reset_exhaustion_flags(
                project_dir, f"TDD iter {iter_n} boundary — {total_filled - _filled_at_last_reset} "
                             "new fill(s) since the last check")
            _filled_at_last_reset = total_filled
            if _cleared:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-admitted "
                         f"{_cleared} previously-exhausted fn(s)/block(s) for "
                         "one more informed attempt.")
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: running cargo test...")
        t0 = time.monotonic()
        rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                  timeout=test_timeout)
        dt = time.monotonic() - t0
        pass_count, fail_count, _ignored = parse_test_counts(output)
        cur_failed = parse_failed_tests(output)
        fail_count, cur_failed, _oracle = _reconcile(
            rc, output, pass_count, fail_count, cur_failed)
        # Batch ratchet. pass_count reflects the PREVIOUS iter's fills;
        # a drop below the best we've measured means that iter net-regressed the
        # cluster (a regression the per-fill guard couldn't catch while the crate
        # was red mid-batch). Restore the best-known src tree and re-test, so the
        # loop always attacks from — and can only exit at — its high-water mark.
        # An oracle shrink beyond the snapshot's own (tests that executed at the
        # snapshot no longer report) is the same net regression, even when the
        # pass count held — the lost binary may have been mostly failing.
        _breach = ratchet_breach(pass_count, _oracle.missing_count,
                                 _bratchet_best, _bratchet_missing)
        if _batch_ratchet and _bratchet_snap is not None and _breach:
            # A crashing binary drops a nondeterministic subset of tests, so a
            # single breached reading can be pure noise; re-sweep the UNCHANGED
            # tree and only a reproduced breach earns the destructive revert
            # (a spurious one would wipe sibling-blocked fills every iter and
            # lock the plateau).
            rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                        timeout=test_timeout)
            pass_count, fail_count, _ignored = parse_test_counts(output)
            cur_failed = parse_failed_tests(output)
            fail_count, cur_failed, _oracle = _reconcile(
                rc, output, pass_count, fail_count, cur_failed)
            _confirm = ratchet_breach(pass_count, _oracle.missing_count,
                                      _bratchet_best, _bratchet_missing)
            if not _confirm:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                         f"breach ({_breach}) NOT reproduced on re-sweep; "
                         "keeping the tree (crash-noise reading).")
            else:
                _rev = restore_src_tree(_bratchet_snap)
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                         f"{_confirm} (last iter's fills net-regressed, "
                         f"confirmed on re-sweep); reverted {_rev} file(s) to "
                         "best state, re-testing.")
                deps.emit("phase_c.tdd.batch_revert", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count, "best_pass": _bratchet_best,
                    "breach": _confirm, "files_reverted": _rev,
                })
                rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                            timeout=test_timeout)
                pass_count, fail_count, _ignored = parse_test_counts(output)
                cur_failed = parse_failed_tests(output)
                fail_count, cur_failed, _oracle = _reconcile(
                    rc, output, pass_count, fail_count, cur_failed)
        # Refresh the best-known snapshot on the first iter and each strict
        # improvement (never on a plateau — the existing best-state snapshot is
        # equivalent and re-reading the tree would be wasted I/O). Oracle veto
        # is regime-scoped (see snapshot_allowed): only a crash-free-vs-crashy
        # comparison is exact; two confounded readings are non-comparable.
        _min_missing = (_oracle.missing_count if _ckpt_min_missing is None
                        else min(_ckpt_min_missing, _oracle.missing_count))
        if _batch_ratchet and (_bratchet_snap is None or pass_count > _bratchet_best) \
                and snapshot_allowed(_oracle.missing_count, _min_missing):
            _snap = snapshot_src_tree(_src_root)
            if _snap is not None:
                _bratchet_best = pass_count
                _bratchet_snap = _snap
                _bratchet_missing = _oracle.missing_count
        # Durable checkpoint of the new-best fill tree so a later heal restores
        # THIS good state (best-tree copy / git ancestor) instead of nuking the
        # file to the baked skeleton. Independent of _batch_ratchet. Never
        # persisted while the oracle is short: a higher pass count over a
        # shrunken suite must not overwrite a full-oracle best tree.
        if deps.checkpoint_fill_tree is not None and pass_count > _checkpoint_best \
                and _oracle.missing_count == 0:
            deps.checkpoint_fill_tree(
                project_dir, cluster.name, target_src_dir, pass_count, iter_n)
            _checkpoint_best = pass_count
        _ckpt_min_missing = _min_missing
        # F6 — measurement provenance: stamp every iter event with the tree it
        # measured (content hash), the oracle size (denominator), and the actual
        # test names, so cross-run pass-count claims are verifiable and oracle
        # drift is detectable.
        _measure = Measurement(
            cluster=cluster.name, iteration=iter_n,
            tree_sha=tree_content_sha(_src_root),
            oracle_size=pass_count + fail_count,
            pass_count=pass_count, fail_count=fail_count,
            passed=parse_test_output(output).passed,
            failed=frozenset(cur_failed))
        _prov = {"tree_sha": _measure.tree_sha, "oracle_size": _measure.oracle_size,
                 "passed": sorted(_measure.passed)[:200],
                 "failed": sorted(_measure.failed)[:200]}
        # Per-binary breakdown so triage can see WHICH test set
        # changed between iters (vanished tests, not just pass→fail flips).
        _bins = summarize_test_binaries(output)
        if _bins:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n} binaries: "
                     + ", ".join(f"{lbl} {p}/{f}" for lbl, p, f in _bins))
        if rc == 0 and not _oracle.intact:
            # cargo says green but part of the expected oracle never reported —
            # a shrunken suite must not close the cluster (FR-024). Fall
            # through to the failing path with the vanished tests as failures.
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: rc==0 but the "
                     f"oracle is short {_oracle.missing_count} test(s) — NOT "
                     "declaring the cluster done.")
        if rc == 0 and _oracle.intact:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: all tests pass "
                 f"({pass_count} passed, {dt:.1f}s). Cluster done.")
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": 0,
                "frontier_size": 0, "kind": "pass",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break
        if rc == -1:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test TIMED OUT "
                 f"({test_timeout}s). Stopping.")
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": -1,
                "frontier_size": 0, "kind": "timeout",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break

        # Improvement metric: did pass count strictly increase since best?
        improved = pass_count > best_pass
        if improved:
            best_fail = fail_count
        # Hook 3 — detect a causal regression BEFORE recording, so the note
        # rides this iter's Attempt (frozen) and surfaces in context(). A test
        # that flipped pass→fail right after last iter's fill is attributable
        # to that fill; write the concrete WRONG-body note for the model.
        _regr_note = ""
        if _regression_notes and fr_path and last_newly_filled:
            _newly_failed = cur_failed - prev_failed
            if _newly_failed:
                _regr_note = deps.record_tdd_regression(
                    fr_path, src_dir, last_newly_filled, _newly_failed,
                    best_pass, pass_count,
                )
        prev_failed = cur_failed
        # Record this iter's measurement on the shared LoopState (metric is
        # pass_count — "higher is better" here, so NO negation). record()
        # updates the floor + stall streak; mirror them into the scalars the
        # log/emit lines below read.
        state.record(Attempt(metric=float(pass_count), iteration=state.iteration,
                              improved=improved, note=_regr_note))
        best_pass = int(state.best_metric)
        stall = state.stalls

        # Attribute failures via the full chain (panics → compile → behavior →
        # classifier → recently_filled → reverse_map). The cluster's in-memory
        # test pairs feed the deterministic behavior path (s117 stable-51/72).
        frontier, kind = deps.attribute_failures(
            output, src_dir, recently_filled,
            test_reverse_map, cluster.name,
            test_texts=dict(cluster_tests),
            extra_failed=frozenset(_oracle.vanished),
        )
        frontier = deps.filter_test_targets(frontier, src_dir)

        # Crash localization: the vanished-test oracle names WHICH tests died;
        # the localizer names WHERE (gdb fast path, ASAN fallback with origin
        # frames). Blamed fns enter the frontier directly — stack evidence
        # outranks reverse-map attribution — their fill prompts receive the
        # rendered CRASH_EVIDENCE, and crash evidence voids prior exhaustion
        # verdicts (a localized crash is new information the earlier attempts
        # never saw).
        crash_texts: dict[FnLocator, str] = {}
        # Gated on NAMED vanished tests, never the bare missing count: an
        # unnamed shortfall is an inventory gap, and sending gdb + an ASAN
        # rebuild after it burns a sweep to conclude "no native fault".
        if _oracle.vanished and deps.localize_crashes is not None:
            _sites = deps.localize_crashes(
                output, test_cmd, target_src_dir, cluster_env)
            crash_texts = crash_evidence_by_locator(tuple(_sites))
            for _site in _sites:
                deps.log(f"  {tag} [phase-c/crash] {_site.binary_label}"
                         f"::{_site.test_name or '?'} {_site.signal} — "
                         f"blamed {_site.blamed_locator}")
                deps.emit("phase_c.crash.localized", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "binary": _site.binary_label, "signal": _site.signal,
                    "test": _site.test_name,
                    "frames": [[f.file_rel, f.line, f.fn_name]
                               for f in _site.all_frames]})
            if not _sites:
                deps.log(f"  {tag} [phase-c/crash] {len(_oracle.vanished)} "
                         "test(s) vanished but no crash could be localized "
                         "(no native fault under gdb, no ASAN report).")
            for _loc in crash_texts:
                frontier.setdefault(_loc, set()).update(_oracle.vanished)

        # Futility escalation: a failing test whose attributed
        # unit-set is identical to last iteration's has been fully served by
        # rewrites that changed nothing — its accessors are the wrong target.
        # Escalate ONCE per test to the producers of the exercised types.
        if deps.escalate_to_producers is not None:
            units_by_test: dict[str, set[FnLocator]] = {}
            for key, tests in frontier.items():
                for tname in tests:
                    units_by_test.setdefault(tname, set()).add(key)
            for tname, units in units_by_test.items():
                prev = prev_test_units.get(tname)
                if (tname not in escalated_tests and prev is not None
                        and prev == frozenset(units)):
                    extra = deps.escalate_to_producers(
                        src_dir, tname, set(units))
                    if extra:
                        escalated_tests.add(tname)
                        for k, ts in extra.items():
                            frontier.setdefault(k, set()).update(ts)
                        deps.emit("phase_c.attribution.escalated", data={
                            "test": tname,
                            "futile_units": sorted(str(u) for u in units),
                            "added": sorted(str(k) for k in extra),
                        })
                prev_test_units[tname] = frozenset(units)

        # Union remaining cluster-active stubs into the frontier so a helper
        # that no failing test attributes directly (called only transitively)
        # still fills in phase-c instead of surviving to the post-TASK3 sweep
        # while its tested callers thrash as "blocked by an unfilled sibling
        # stub". order_frontier sorts leaf-first so helpers fill ahead of their
        # callers; the D3 filter + permanently_exhausted still bound re-entry.
        frontier = union_unattributed_stubs(
            frontier, deps.scan_unimplemented_for_cluster(target_src_dir, cluster))

        # D3 — re-entry filter for fns that already exhausted the per-fn
        # fix loop in a prior iter (avoid burning compute on the same fn
        # with no new context).
        # Crash evidence voids exhaustion: the prior attempts never saw a
        # localized faulting stack, so "nothing left to try" no longer holds.
        permanently_exhausted -= set(crash_texts)
        eligible: Frontier = {}
        skipped_perm: int = 0
        for key, tests in frontier.items():
            if key in permanently_exhausted:
                notes_now = deps.count_relevant_notes(fr_path, key)
                if total_filled == last_total_filled and \
                   notes_now <= last_notes_count.get(key, 0):
                    skipped_perm += 1
                    continue
                permanently_exhausted.discard(key)
            eligible[key] = tests

        # F4 core-first dispatch: a unit whose every failing test panics inside
        # a DIFFERENT still-stubbed producer can't be helped by filling it —
        # defer it (NOT exhaust) so the producer gets the LLM budget first. This
        # is stateless: recomputed each iter from the current stub set, so a
        # deferred consumer is re-admitted the moment its producer fills.
        _stub_keys = frozenset(
            deps.scan_unimplemented_for_cluster(target_src_dir, cluster))
        eligible, _deferred = defer_blocked_consumers(eligible, _stub_keys)
        if _deferred:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: deferred "
                     f"{len(_deferred)} consumer(s) blocked by still-stubbed "
                     "producer(s) (re-admitted when the producer fills).")
            deps.emit("phase_c.dispatch.deferred", data={
                "cluster": cluster.name, "iter": iter_n,
                "deferred": {f"{f}::{fn}": sorted(f"{bf}::{bfn}"
                                                  for bf, bfn in blk)
                             for (f, fn), blk in _deferred.items()}})

        # Cause-C layout retrigger (proactive, not stall-gated): expected
        # tests VANISHING sweep after sweep with a clean compile is the
        # fingerprint of a frozen unsafe scheme whose filled bodies crash the
        # test binaries — per-fn re-fills can't converge on a shared pointer
        # scheme, so re-author the implicated frozen component instead of
        # burning the stall budget on it.
        if _oracle.missing_count > 0:
            _vanish_streak += 1
            # A shrunk oracle with a clean compile is either the frozen-scheme
            # crash class or a transient. Waiting for the streak proves
            # persistence at the cost of a full fill pass (hours of GPU spent
            # on tests that cannot report verdicts); an immediate re-sweep
            # (<1s) answers the same transient-vs-real question now — a crash
            # that reproduces back-to-back is deterministic, so the streak is
            # treated as complete this iter. EXCEPT right after a successful
            # re-author: its surgical revert leaves the component's fns as
            # stubs, and a stub panic inside drop/vtable code aborts the test
            # binary — the very next sweep shrinks REGARDLESS of the new
            # layout's quality. Firing then burns the capped attempt budget
            # with zero re-fill evidence (observed live: attempts 1 and 2 in
            # back-to-back iters). The fills of THIS iter must run first.
            if (_vanish_streak < _crash_retrigger_after
                    and not _retrig_cooldown
                    and deps.retrigger_layout is not None):
                _rc2, _out2 = deps.run_cargo(
                    test_cmd, target_src_dir, cluster_env, timeout=test_timeout)
                _p2, _f2, _ig2 = parse_test_counts(_out2)
                _, _, _oracle2 = _reconcile(
                    _rc2, _out2, _p2, _f2, parse_failed_tests(_out2))
                if _oracle2.missing_count > 0:
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: oracle shrink "
                        "reproduced on immediate re-sweep — crash is "
                        "deterministic, retriggering now instead of waiting "
                        "out the sweep streak.")
                    _vanish_streak = _crash_retrigger_after
        else:
            _vanish_streak = 0
        _retrig_cooldown = False   # one iter of fills has now had its chance
        if (_vanish_streak >= _crash_retrigger_after
                and deps.retrigger_layout is not None):
            _vanish_streak = 0   # re-arm only after another full streak
            _suspects: set[FnLocator] = set()
            for _vt in _oracle.vanished:
                _bare = _vt.split("::")[-1]
                for _k in (test_reverse_map.get(_bare)
                           or test_reverse_map.get(_vt) or set()):
                    _suspects.add(FnLocator(*_k))
            _retrig_c = deps.retrigger_layout(
                project_dir, module, cluster.name, target_src_dir, output,
                crash_evidence=CrashEvidence(
                    vanished=frozenset(_oracle.vanished),
                    suspect_fns=frozenset(_suspects),
                    consecutive_sweeps=_crash_retrigger_after))
            if _retrig_c.fired:
                _retrig_cooldown = True
            if _retrig_c.fired and _retrig_c.succeeded:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: CRASH layout "
                         f"retrigger fired (cause={_retrig_c.cause}) — reverted "
                         f"{_retrig_c.reverted_count} fn(s) to re-fill under "
                         "the new layout; resetting stall budget.")
                deps.emit("phase_c.layout_retrigger.fired", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "cause": _retrig_c.cause,
                    "reverted_count": _retrig_c.reverted_count,
                })
                state.stalls = 0
                stall = 0
                continue

        # Exit condition: stall budget exhausted on the cluster-level
        # pass_count floor. Fires regardless of whether `eligible` is
        # empty — pass_count is the only metric that captures real-world
        # progress, so when it hasn't moved for `max_stalls` iters the
        # loop isn't making progress no matter how busy the frontier
        # looks. Pre-fix, an unfixable fn that stayed in the frontier
        # forever would override the stall check and burn compute
        # indefinitely. The frontier is now a hint about WHERE to look,
        # not a license to keep iterating.
        if stall >= max_stalls:
            # F11 M3: before giving up, check whether the plateau is a private
            # LAYOUT problem (t1.5 left a component frozen on the original
            # scaffold, and this cluster's fills are fighting it) rather than
            # N independently-hard fns. A successful re-author + surgical
            # revert resets the stall counter and lets the loop continue
            # against a layout the model can actually work with; a capped or
            # failed attempt falls through to the unchanged exit below.
            if deps.retrigger_layout is not None:
                _retrig = deps.retrigger_layout(
                    project_dir, module, cluster.name, target_src_dir, output)
                if _retrig.fired and _retrig.succeeded:
                    deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: layout "
                         f"retrigger fired (cause={_retrig.cause}) — reverted "
                         f"{_retrig.reverted_count} fn(s) to re-fill under the "
                         "new layout; resetting stall budget.")
                    deps.emit("phase_c.layout_retrigger.fired", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "cause": _retrig.cause,
                        "reverted_count": _retrig.reverted_count,
                    })
                    state.stalls = 0
                    stall = 0
                    continue
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass_count plateau "
                 f"{stall} iter(s) at best={best_pass}; stopping "
                 f"({len(eligible)} fn(s) still on frontier — will be "
                 "marked exhausted). best pass="
                 f"{best_pass} fail={best_fail}.")
            _log_attribution_gap(deps, tag, iter_n, fail_count, cur_failed,
                                 len(frontier), kind)
            for _stuck_key in eligible:
                permanently_exhausted.add(_stuck_key)
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": "stall_exit",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            break
        if not eligible:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: no actionable "
                 f"frontier this iter but stall={stall}/{max_stalls} — "
                 f"continuing (pass={pass_count}, fail={fail_count}, "
                 f"improved={improved}).")
            _log_attribution_gap(deps, tag, iter_n, fail_count, cur_failed,
                                 len(frontier), kind)
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": 0, "kind": "no_frontier_continue",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            # Nothing to do this iter — loop back to re-run cargo test.
            # (Mostly a no-op, but keeps the loop honest: if a flaky
            # failure or test ordering surfaced a fn next iter, we'll
            # pick it up.)
            continue

        ordered = deps.order_frontier(eligible, leaf_counts)
        frontier_keys = {key for key, _ in ordered}
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test rc={rc} "
             f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
             f"oracle={_measure.oracle_size} "
             f"(improved={improved}, stall={stall}/{max_stalls}); "
             f"frontier={len(eligible)} fn(s) ({kind}), "
             f"{skipped_perm} skipped as exhausted.")
        deps.emit("phase_c.tdd.iter", data={
                **_prov,
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind,
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
            "binaries": [[lbl, p, f] for lbl, p, f in _bins],
        })

        # Re-fill targets this iter = frontier fns already filled at iter start
        # (not stubs) → the engine will behaviorally re-fill them.
        _stubs_before = set(deps.scan_unimplemented_functions(src_dir))
        _refill_targets = frontier_keys - _stubs_before
        # Drive the per-fn fill machinery with this frontier.
        sub = deps.task2_fill_fns_one_by_one(
            project_dir=project_dir,
            target_src_dir=target_src_dir,
            tests_dir_files=tests_dir_files,
            module_name=module,
            cluster_name=cluster.name,
            cluster=cluster,
            check_cmd=check_cmd,
            cluster_env=cluster_env,
            max_passes=1,
            leaf_name_counts=leaf_counts,
            fr_path=fr_path,
            tdd_frontier_keys=frontier_keys,
            tdd_frontier_tests=eligible,   # Plan 10 U2: {key: {tests}} to validate
            tdd_test_cmd=test_cmd,         # Plan 10 U2: scoped cluster test cmd
            skip_end_pass_test=True,
            crash_evidence=crash_texts or None,
        )
        total_filled += sub.fns_filled
        total_attempts += sub.attempts
        total_bytes += sub.bytes_filled
        errors.extend(sub.errors)

        # Update recently_filled ring buffer with this iter's actual fills.
        # We don't have the exact list from the sub-result, so use the
        # frontier keys that are NO LONGER in the unimplemented set after
        # the pass — that's the set of fns that materially changed.
        cur_stubs = set(deps.scan_unimplemented_functions(src_dir))
        newly_filled = [k for k in frontier_keys if k not in cur_stubs]
        last_newly_filled = newly_filled   # hook 3: next iter attributes regressions to these
        for k in newly_filled:
            recently_filled.append(k)
        if len(recently_filled) > recently_filled_max:
            recently_filled = recently_filled[-recently_filled_max:]
        for key in frontier_keys:
            if key in cur_stubs:
                # First-fill attempt left it a stub → exhausted (existing).
                permanently_exhausted.add(key)
                last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
            elif key in _refill_targets:
                # residual: a behavioral re-fill was attempted (fn was
                # filled at iter start). It reappears here only while its test
                # keeps failing → count the attempt and exhaust it at the cap so
                # it stops re-attempting every iter. A re-fill that FIXED its test
                # leaves the frontier next iter and never reaches the cap.
                refill_attempts[key] = refill_attempts.get(key, 0) + 1
                if refill_attempts[key] >= _refill_max:
                    permanently_exhausted.add(key)
                    last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
        last_total_filled = total_filled

    return ClusterFillResult(
        fns_filled=total_filled,
        bytes_filled=total_bytes,
        attempts=total_attempts,
        remaining=len(deps.scan_unimplemented_for_cluster(
            target_src_dir, cluster)),
        errors=tuple(errors),
    )
