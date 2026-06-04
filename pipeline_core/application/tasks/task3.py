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


def task3(project_dir, test_command, iteration, output_dir, genesis_path, deps: Task3Deps):
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

        per_cluster_results: list[dict] = []
        aggregated_output_chunks: list[str] = []
        any_failed = False
        any_incomplete = False
        agg_pass = agg_fail = agg_ignored = agg_expected = 0
        primary_test_output = ""
        primary_cluster = None

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
            })
            aggregated_output_chunks.append(
                f"=== cluster: {cluster.name} (rc={rc}, "
                f"{status}, {pass_n}/{expected_n} passed, {fail_n} failed, "
                f"{ign_n} ignored) ===\n{output}\n"
            )
            agg_pass += pass_n
            agg_fail += fail_n
            agg_ignored += ign_n
            agg_expected += expected_n
            if status != "pass":
                any_failed = any_failed or status == "fail"
                any_incomplete = any_incomplete or status == "incomplete"
                # Keep this cluster's output AND identity as primary for the inline
                # retry — it has the actionable signal, and the retry must rebuild
                # under THIS cluster's toolchain/env to reproduce its failure (R9).
                if not primary_test_output:
                    primary_test_output = output
                    primary_cluster = cluster

        aggregated_output = "".join(aggregated_output_chunks)
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
            if retry_passed or retry_output != primary_test_output:
                # Re-run all clusters to verify the retry's fixes hold
                # across the corpus, not just the primary failing cluster.
                # Recurse once.
                return task3(
                    project_dir, test_command, iteration,
                    output_dir, genesis_path, deps,
                )

        done["data"] = {
            "passed": False, "pass_count": agg_pass,
            "fail_count": agg_fail, "expected_count": agg_expected,
            "per_cluster": per_cluster_results,
        }
        return False, aggregated_output
