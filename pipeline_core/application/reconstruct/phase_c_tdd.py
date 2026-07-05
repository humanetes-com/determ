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
    restore_src_tree,
    snapshot_src_tree,
)
from pipeline_core.domain.attribution import union_unattributed_stubs
from pipeline_core.domain.models import BuildCluster, FnLocator, Frontier
from pipeline_core.domain.refine import Attempt, LoopState
from pipeline_core.domain.test_results import (
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
    run_cargo: Callable[..., Any]
    scan_unimplemented_for_cluster: Callable[..., Any]
    scan_unimplemented_functions: Callable[..., Any]
    task2_fill_fns_one_by_one: Callable[..., Any]


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
) -> dict[str, Any]:
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
        return {"fns_filled": 0, "bytes_filled": 0, "attempts": 0,
                "remaining": len(deps.scan_unimplemented_for_cluster(
                    target_src_dir, cluster)),
                "errors": []}
    test_cmd = test_cmd_builder(test_stems)
    test_timeout = int(os.getenv("PHASE_C_TEST_TIMEOUT", "120"))
    # max_iters is legacy — only the 10k backstop in the loop body honors it.
    # The real terminator is floor-stall on pass_count (max_stalls below).
    max_stalls = deps.effective_stalls(int(os.getenv("TDD_MAX_STALLS", "3")),
                                   deps.converge_in_cluster(cluster) or module)

    deps.log(f"  {tag} [phase-c/tdd] driver=tdd, "
         f"max_stalls={max_stalls}, timeout={test_timeout}s, "
         f"stems={len(test_stems)}")

    permanently_exhausted: set[FnLocator] = set()
    last_notes_count: dict[FnLocator, int] = {}
    last_total_filled = 0
    # debt #46 residual: behavioral re-fill targets (frontier fns already filled
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
    # Debt #45 — TDD-iter batch ratchet: snapshot the src tree at each pass_count
    # high-water; restore it when a later iter's fills net-regress the count.
    _batch_ratchet = os.getenv("PHASE_C_TDD_BATCH_RATCHET", "1") not in ("", "0", "false")
    _src_root = target_src_dir / "src"
    _bratchet_best = -1
    _bratchet_snap: dict[str, str] | None = None
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
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: running cargo test...")
        t0 = time.monotonic()
        rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                  timeout=test_timeout)
        dt = time.monotonic() - t0
        pass_count, fail_count, _ignored = parse_test_counts(output)
        cur_failed = parse_failed_tests(output)
        # Debt #45 — batch ratchet. pass_count reflects the PREVIOUS iter's fills;
        # a drop below the best we've measured means that iter net-regressed the
        # cluster (a regression the per-fill guard couldn't catch while the crate
        # was red mid-batch). Restore the best-known src tree and re-test, so the
        # loop always attacks from — and can only exit at — its high-water mark.
        if _batch_ratchet and _bratchet_snap is not None and pass_count < _bratchet_best:
            _rev = restore_src_tree(_bratchet_snap)
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                     f"pass_count {pass_count}<{_bratchet_best} (last iter's fills "
                     f"net-regressed); reverted {_rev} file(s) to best state, "
                     "re-testing.")
            deps.emit("phase_c.tdd.batch_revert", data={
                "cluster": cluster.name, "iter": iter_n,
                "pass_count": pass_count, "best_pass": _bratchet_best,
                "files_reverted": _rev,
            })
            rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                        timeout=test_timeout)
            pass_count, fail_count, _ignored = parse_test_counts(output)
            cur_failed = parse_failed_tests(output)
        # Refresh the best-known snapshot on the first iter and each strict
        # improvement (never on a plateau — the existing best-state snapshot is
        # equivalent and re-reading the tree would be wasted I/O).
        if _batch_ratchet and (_bratchet_snap is None or pass_count > _bratchet_best):
            _snap = snapshot_src_tree(_src_root)
            if _snap is not None:
                _bratchet_best = pass_count
                _bratchet_snap = _snap
        # Debt #44 — per-binary breakdown so triage can see WHICH test set
        # changed between iters (vanished tests, not just pass→fail flips).
        _bins = summarize_test_binaries(output)
        if _bins:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n} binaries: "
                     + ", ".join(f"{lbl} {p}/{f}" for lbl, p, f in _bins))
        if rc == 0:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: all tests pass "
                 f"({pass_count} passed, {dt:.1f}s). Cluster done.")
            deps.emit("phase_c.tdd.iter", data={
                "cluster": cluster.name, "iter": iter_n, "rc": 0,
                "frontier_size": 0, "kind": "pass",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break
        if rc == -1:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test TIMED OUT "
                 f"({test_timeout}s). Stopping.")
            deps.emit("phase_c.tdd.iter", data={
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
        )
        frontier = deps.filter_test_targets(frontier, src_dir)

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
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass_count plateau "
                 f"{stall} iter(s) at best={best_pass}; stopping "
                 f"({len(eligible)} fn(s) still on frontier — will be "
                 "marked exhausted). best pass="
                 f"{best_pass} fail={best_fail}.")
            for _stuck_key in eligible:
                permanently_exhausted.add(_stuck_key)
            deps.emit("phase_c.tdd.iter", data={
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
            deps.emit("phase_c.tdd.iter", data={
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
             f"(improved={improved}, stall={stall}/{max_stalls}); "
             f"frontier={len(eligible)} fn(s) ({kind}), "
             f"{skipped_perm} skipped as exhausted.")
        deps.emit("phase_c.tdd.iter", data={
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind,
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
            "binaries": [[lbl, p, f] for lbl, p, f in _bins],
        })

        # Re-fill targets this iter = frontier fns already filled at iter start
        # (not stubs) → the engine will behaviorally re-fill them (debt #46).
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
        )
        total_filled += sub.get("fns_filled", 0)
        total_attempts += sub.get("attempts", 0)
        total_bytes += sub.get("bytes_filled", 0)
        errors.extend(sub.get("errors", []))

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
                # debt #46 residual: a behavioral re-fill was attempted (fn was
                # filled at iter start). It reappears here only while its test
                # keeps failing → count the attempt and exhaust it at the cap so
                # it stops re-attempting every iter. A re-fill that FIXED its test
                # leaves the frontier next iter and never reaches the cap.
                refill_attempts[key] = refill_attempts.get(key, 0) + 1
                if refill_attempts[key] >= _refill_max:
                    permanently_exhausted.add(key)
                    last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
        last_total_filled = total_filled

    return {
        "fns_filled": total_filled,
        "bytes_filled": total_bytes,
        "attempts": total_attempts,
        "remaining": len(deps.scan_unimplemented_for_cluster(
            target_src_dir, cluster)),
        "errors": errors,
    }
