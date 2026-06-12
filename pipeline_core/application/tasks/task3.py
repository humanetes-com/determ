"""TASK 3 — per-cluster strict-verdict test runner (validation use case).

Extracted from pipeline.py (slice 5b). Body verbatim; cross-cutting infra (log/emit) and
the pure domain helpers (`_parse_test_counts`, `validate_task3_output_strict`,
`OUTPUT_SEPARATOR`) are imported, while the genuinely pipeline-coupled collaborators
(phase tracking, cluster loading, cargo env, the inline-retry fix loop, the legacy path)
are injected via :class:`Task3Deps`. No import of pipeline.py.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.test_results import (
    OUTPUT_SEPARATOR,
    _parse_test_counts,
    validate_task3_output_strict,
)
from ...log import emit, log


@dataclass(frozen=True, slots=True)
class Task3Deps:
    """Pipeline-coupled collaborators TASK 3 needs, injected by the composition root."""

    phase: Callable[..., Any]                       # _phase context-manager factory
    load_baked_clusters: Callable[[Path], list]     # _load_baked_clusters
    task3_legacy_single: Callable[..., Any]         # _task3_legacy_single fallback
    cargo_env: Callable[[], dict]                   # _cargo_env
    parse_cmd: Callable[[str], list]                # _parse_cmd
    count_active_tests_for_cluster: Callable[[Path, Any], int]
    store_prompt_content: Callable[[str], Any]      # _store_prompt_content
    task3_inline_retry: Callable[..., Any]          # _task3_inline_retry
    read: Callable[[Any], str]
    write: Callable[[Any, str], None]
    test_timeout: int


def _should_reverify(primary_output: str, retry_output: str, retry_passed: bool,
                     budget_remaining: int) -> bool:
    """Whether to re-run TASK 3 across all clusters after an inline retry.

    Gate on STABLE test COUNTS, never raw cargo text: cargo output is not
    byte-deterministic (test ordering/timing), so a no-op retry "changes" the
    text — the old `retry_output != primary_test_output` check then recursed
    forever (RecursionError). Re-verify only on genuine progress (retry passed,
    or more tests pass, or fewer fail — the last covers a build-fix that makes
    the tree compile so more tests run) AND while the budget lasts. Exhausted
    budget → caller falls through to the FAIL verdict → TASK 4.
    """
    if budget_remaining <= 0:
        return False
    if retry_passed:
        return True
    init_p, init_f, _ = _parse_test_counts(primary_output)
    new_p, new_f, _ = _parse_test_counts(retry_output)
    return new_p > init_p or new_f < init_f


def run_clusters_strict(project_dir: Path, clusters: list,
                        deps: Task3Deps) -> tuple[bool, str, list[dict]]:
    """Run every baked cluster under its own toolchain + env_extra and apply
    the FR-024 strict verdict (`validate_task3_output_strict`, including the
    expected-count "incomplete" case). THE single authority for "all tests
    pass": task3 and the post-task3 fix loop's check both gate here —
    validating with one bare cargo command is the false-SUCCESS bug
    (2026-06-12 microcrate: default cluster green, extra_cap 3 failing).

    Returns (all_pass, aggregated_output, per_cluster_results). Each result
    dict carries the cluster's raw `output`; callers strip that key before
    emitting events.
    """
    per_cluster_results: list[dict] = []
    aggregated_output_chunks: list[str] = []
    all_pass = True
    for cluster in sorted(clusters, key=lambda c: c.name):
        cmd_parts = ["cargo"]
        if cluster.toolchain:
            cmd_parts.append(f"+{cluster.toolchain}")
        cmd_parts += ["test", "--no-fail-fast"]
        cluster_cmd = shlex.join(cmd_parts)
        cluster_env = deps.cargo_env()
        cluster_env.update(cluster.env_extra or {})
        log(f"  [task3/{cluster.name}] running {cluster_cmd}")
        t_c = time.monotonic()
        try:
            result = subprocess.run(
                deps.parse_cmd(cluster_cmd),
                cwd=project_dir, capture_output=True, text=True,
                timeout=deps.test_timeout, env=cluster_env,
            )
            rc = result.returncode
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except subprocess.TimeoutExpired:
            rc = -1
            stdout = ""
            stderr = (f"TIMEOUT: cluster '{cluster.name}' tests exceeded "
                      f"{deps.test_timeout}s")
            log(f"  [task3/{cluster.name}] {stderr}")
        dt = time.monotonic() - t_c
        output = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        pass_n, fail_n, ign_n = _parse_test_counts(output)
        expected_n = deps.count_active_tests_for_cluster(project_dir, cluster)
        status, reason = validate_task3_output_strict(
            pass_n, fail_n, ign_n, expected_n, cluster.name,
        )
        log(f"  [task3/{cluster.name}] {status}: {reason} "
            f"(rc={rc}, {dt:.1f}s)")
        per_cluster_results.append({
            "cluster": cluster.name,
            "status": status,
            "rc": rc,
            "pass": pass_n,
            "fail": fail_n,
            "ignored": ign_n,
            "expected": expected_n,
            "duration_s": dt,
            "output": output,
        })
        aggregated_output_chunks.append(
            f"=== cluster: {cluster.name} (rc={rc}, "
            f"{status}, {pass_n}/{expected_n} passed, {fail_n} failed, "
            f"{ign_n} ignored) ===\n{output}\n"
        )
        if status != "pass":
            all_pass = False
    return all_pass, "".join(aggregated_output_chunks), per_cluster_results


def task3(project_dir: Path, test_command: str, iteration: int | str,
          output_dir: Path | str, genesis_path: Path, deps: Task3Deps,
          _reverify_remaining: int = 2) -> Any:
    """TASK 3 — per-cluster strict-verdict test runner.

    FR-024: SUCCESS only when every test in every cluster genuinely runs
    and passes. For each baked cluster:
      1. Compose `cargo test --no-fail-fast` with the cluster's toolchain
         and `env_extra` (RUSTFLAGS etc).
      2. Run, parse (pass, fail, ignored).
      3. Compute expected_count = #[test] fns active under this cluster's
         cfg (`_count_active_tests_for_cluster`).
      4. Accept iff `pass + ignored + fail == expected` AND `fail == 0`.

    Aggregate: SUCCESS only if every cluster accepts. Any cluster failing
    or reporting fewer than expected tests → fail → loop to TASK 4.

    The `test_command` parameter is retained for back-compat but ignored
    when baked clusters are available. When the workspace has no baked
    clusters (e.g. fresh non-skeleton-backend run) we fall back to the
    legacy single-shot path using the discovered `test_command`.
    """
    log(f"\nTASK 3: Running tests (iteration {iteration})")
    output_file = Path(output_dir) / f"tests_output_{iteration}.txt"
    _attempt = iteration if isinstance(iteration, int) else None

    clusters = deps.load_baked_clusters(project_dir)
    if not clusters:
        # Legacy fallback: no baked cluster manifest, run once with the
        # discovered command. Used for non-skeleton-backend pipelines.
        return deps.task3_legacy_single(project_dir, test_command, iteration,
                                        output_dir, genesis_path)

    with deps.phase("task", task="task3", attempt=_attempt,
                    data={"command": "per-cluster", "iteration": iteration,
                          "clusters": [c.name for c in clusters]}) as done:
        emit("test.start", attempt=_attempt,
             data={"command": "per-cluster",
                   "clusters": [c.name for c in clusters]})
        _t_start = time.monotonic()

        _all_pass, aggregated_output, cluster_results = run_clusters_strict(
            project_dir, clusters, deps)
        # Strip the raw per-cluster output before anything event-shaped.
        per_cluster_results = [
            {k: v for k, v in r.items() if k != "output"} for r in cluster_results]
        any_failed = any(r["status"] == "fail" for r in cluster_results)
        any_incomplete = any(r["status"] == "incomplete" for r in cluster_results)
        agg_pass = sum(r["pass"] for r in cluster_results)
        agg_fail = sum(r["fail"] for r in cluster_results)
        agg_ignored = sum(r["ignored"] for r in cluster_results)
        agg_expected = sum(r["expected"] for r in cluster_results)
        # Primary = first non-pass cluster: its output has the actionable signal,
        # and the inline retry must rebuild under ITS toolchain/env to reproduce
        # the failure (R9).
        primary_test_output = ""
        primary_cluster = None
        _cluster_by_name = {c.name: c for c in clusters}
        for r in cluster_results:
            if r["status"] != "pass":
                primary_test_output = r["output"]
                primary_cluster = _cluster_by_name[r["cluster"]]
                break
        genesis = deps.read(genesis_path)
        deps.write(output_file,
                   f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n"
                   f"{OUTPUT_SEPARATOR}\n{aggregated_output}")

        all_pass = not any_failed and not any_incomplete
        log(f"  TASK 3 aggregate: {agg_pass}/{agg_expected} passed, "
            f"{agg_fail} failed, {agg_ignored} ignored across "
            f"{len(per_cluster_results)} cluster(s) — "
            f"{'PASS' if all_pass else ('FAIL' if any_failed else 'INCOMPLETE')}.")

        if all_pass:
            emit("test.pass", duration_ms=int(
                (time.monotonic() - _t_start) * 1000),
                 data={"per_cluster": per_cluster_results,
                       "pass_count": agg_pass, "fail_count": agg_fail,
                       "ignored_count": agg_ignored,
                       "expected_count": agg_expected,
                       "output": deps.store_prompt_content(aggregated_output)})
            done["data"] = {
                "passed": True, "pass_count": agg_pass,
                "fail_count": agg_fail, "expected_count": agg_expected,
                "per_cluster": per_cluster_results,
            }
            return True, aggregated_output

        emit("test.fail", duration_ms=int(
            (time.monotonic() - _t_start) * 1000),
             data={"per_cluster": per_cluster_results,
                   "pass_count": agg_pass, "fail_count": agg_fail,
                   "ignored_count": agg_ignored,
                   "expected_count": agg_expected,
                   "reason": "incomplete" if any_incomplete and not any_failed
                             else "failed_tests",
                   "output": deps.store_prompt_content(aggregated_output)})

        # FR-024 inline retry on the primary failing cluster. Uses the
        # full attribution chain (panics → compile → classifier →
        # recently_filled → reverse_map) to identify fns to refix.
        if primary_test_output:
            retry_passed, retry_output = deps.task3_inline_retry(
                project_dir=project_dir,
                test_command="cargo test --no-fail-fast",
                iteration=iteration,
                initial_test_output=primary_test_output,
                cluster=primary_cluster,
            )
            # Re-verify across all clusters ONLY on genuine progress, and only
            # while the budget lasts (see _should_reverify).
            if _should_reverify(primary_test_output, retry_output, retry_passed,
                                _reverify_remaining):
                r_passed, r_output = task3(
                    project_dir, test_command, iteration,
                    output_dir, genesis_path, deps,
                    _reverify_remaining=_reverify_remaining - 1,
                )
                # The recursion emitted its own fully-populated task.done; this
                # level's event must still carry the verdict — a task.done with
                # no `passed` key reads as passed=None to the auditor (C9).
                r_pass, r_fail, _r_ign = _parse_test_counts(r_output)
                done["data"] = {
                    "passed": r_passed, "reverified": True,
                    "pass_count": r_pass, "fail_count": r_fail,
                    "expected_count": agg_expected,
                }
                return r_passed, r_output

        done["data"] = {
            "passed": False, "pass_count": agg_pass,
            "fail_count": agg_fail, "expected_count": agg_expected,
            "per_cluster": per_cluster_results,
        }
        return False, aggregated_output
