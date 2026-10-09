"""TASK 3 — per-cluster strict-verdict test runner (validation use case).

Extracted from pipeline.py. Body verbatim; cross-cutting infra (log/emit) and
the pure domain helpers (`parse_test_counts`, `validate_task3_output_strict`,
`OUTPUT_SEPARATOR`) are imported, while the genuinely pipeline-coupled collaborators
(phase tracking, cluster loading, cargo env, the inline-retry fix loop, the legacy path)
are injected via :class:`Task3Deps`. No import of pipeline.py.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.test_results import join_capture

from ...application.reconstruct.attribution import not_body_repairable_tests
from ...domain.cluster_env import overlay_cluster_env
from ...domain.reference_floor import (
    ReferenceMeasurement,
    judge_cluster_binaries,
)
from ...domain.test_results import (
    OUTPUT_SEPARATOR,
    ClusterStatus,
    cluster_failure_is_actionable,
    compile_gate_reason,
    parse_binary_results,
    parse_failed_tests,
    parse_test_counts,
    rank_repair_candidates,
    validate_task3_output_strict,
)
from ...log import emit, log


@dataclass(frozen=True, slots=True)
class Task3Deps:
    """Pipeline-coupled collaborators TASK 3 needs, injected by the composition root."""

    phase: Callable[..., AbstractContextManager[dict[str, Any]]]  # _phase factory
    load_baked_clusters: Callable[[Path], list[Any]]  # _load_baked_clusters
    task3_legacy_single: Callable[..., tuple[bool, str]]  # _task3_legacy_single fallback
    cargo_env: Callable[[], dict[str, str]]         # _cargo_env
    parse_cmd: Callable[[str], list[str]]           # _parse_cmd
    count_active_tests_for_cluster: Callable[[Path, Any], int]
    store_prompt_content: Callable[[str], Any]      # _store_prompt_content
    task3_inline_retry: Callable[..., tuple[bool, str]]  # _task3_inline_retry
    read: Callable[[Any], str]
    write: Callable[[Any, str], None]
    test_timeout: int
    # The pristine tree's verdict vector, or None when no reference could be
    # measured. A CALLABLE because the measurement is per-run and this record is
    # built once at composition time, before any project_dir is known.
    #
    # Deliberately NOT wired to `count_active_tests_for_cluster`. The reference
    # announces 99 tests per cluster on `anyhow` while the reconstruction
    # announces 129 (nightly) and 149 (stable) — the difference is the
    # `src/*.gen_tests_*.rs` augmentation, which upstream does not have — so
    # taking the expected count from the reference would undershoot by 30–50 and
    # break the announcement guard in the opposite direction from the one it was
    # already broken in. The denominator stays with `find_unreported_tests`,
    # which reconciles cargo's own `running N` against its own silence and needs
    # no inventory at all.
    reference: Callable[[Path], ReferenceMeasurement | None] = lambda _p: None
    # The env for ONE named cluster (RUSTFLAGS appended, RUSTUP_TOOLCHAIN set
    # when required). Bound to cluster_cargo_env in composition. None builds
    # it from cargo_env() through the same overlay rule
    # (`domain.cluster_env.overlay_cluster_env`), for tests that inject a fake
    # result through env_extra; the dict.update it replaced dropped
    # -A warnings and skipped the toolchain on a live nightly cluster.
    cluster_env: Callable[[Any], dict[str, str]] | None = None
    # (project_dir, cluster, pass count, expected count): the held-out
    # agreement reading on the tree this phase just tested, printed beside
    # its oracle tally (`application/behaviour_agreement.py`). Counts only;
    # nothing it returns is read here. None takes no reading.
    measure_agreement: Callable[[Path, Any, int, int], object] | None = None
    # (project_dir, cluster name) -> the red ledger t2a wrote at that
    # cluster's end (`adapters/red_ledger_store`): printed beside a red
    # cluster's verdict, so the FAIL names each red test and what held it.
    # None prints nothing.
    red_ledger: Callable[[Path, str], Mapping[str, Any]] | None = None


def red_ledger_lines(stored: Mapping[str, Any], *, limit: int = 40) -> list[str]:
    """The stored red ledger as task 3 prints it: the per-class count and
    one line per red test, or one line saying t2a wrote none."""
    entries = list(stored.get("entries") or [])
    if not entries:
        return ["red ledger: none written by t2a for this cluster"]
    counts: dict[str, int] = {}
    for e in entries:
        counts[str(e.get("class"))] = counts.get(str(e.get("class")), 0) + 1
    lines = [f"red ledger (t2a {stored.get('close_kind') or 'end'}): "
             f"{len(entries)} red test(s): "
             + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
    lines.extend(f"  {e.get('class')}: {e.get('test')} — {e.get('why')}"
                 for e in entries[:limit])
    return lines


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
    init_p, init_f, _ = parse_test_counts(primary_output)
    new_p, new_f, _ = parse_test_counts(retry_output)
    return new_p > init_p or new_f < init_f


def run_clusters_strict(project_dir: Path, clusters: list[Any],
                        deps: Task3Deps) -> tuple[bool, str, list[dict[str, Any]]]:
    """Run every baked cluster under its own toolchain + env_extra and apply
    the strict verdict (`validate_task3_output_strict`, including the
    expected-count "incomplete" case). THE single authority for "all tests
    pass": task3 and the post-task3 fix loop's check both gate here —
    validating with one bare cargo command is the false-SUCCESS bug
    (2026-06-12 microcrate: default cluster green, extra_cap 3 failing).

    Returns (all_pass, aggregated_output, per_cluster_results). Each result
    dict carries the cluster's raw `output`; callers strip that key before
    emitting events.

    A cluster whose only failures are ones the PRISTINE tree also fails passes
    here. `anyhow`'s `ui` is a trybuild harness whose assertion is about rustc's
    own diagnostic rendering, and upstream regenerates its `.stderr` fixtures
    per nightly; on 2026-08-06 the reconstruction scored `nightly` 128/1 and the
    pristine `1dbe1862 Release 1.0.104` scored 98/1 on the same machine, failing
    the same test. Holding `all_pass` false on that forever is what sent ten
    module iterations of model calls at a guaranteed exit 1. Only test FAILURES
    are excused: an INCOMPLETE cluster (tests announced and never reported, a
    crashed binary) keeps its status, because the reference says nothing about
    whether this tree's binaries survived.
    """
    per_cluster_results: list[dict[str, Any]] = []
    aggregated_output_chunks: list[str] = []
    reference = deps.reference(project_dir)
    all_pass = True
    for cluster in sorted(clusters, key=lambda c: c.name):
        cmd_parts = ["cargo"]
        if cluster.toolchain:
            cmd_parts.append(f"+{cluster.toolchain}")
        cmd_parts += ["test", "--no-fail-fast"]
        cluster_cmd = shlex.join(cmd_parts)
        if deps.cluster_env is not None:
            cluster_env = deps.cluster_env(cluster)
        else:
            cluster_env = deps.cargo_env()
            overlay_cluster_env(cluster_env, cluster)
        log(f"  [task3/{cluster.name}] running {cluster_cmd}")
        t_c = time.monotonic()
        try:
            result = subprocess.run(
                deps.parse_cmd(cluster_cmd),
                cwd=project_dir, capture_output=True, text=True, errors="replace",
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
        output = join_capture(stdout, stderr)
        pass_n, fail_n, ign_n = parse_test_counts(output)
        expected_n = deps.count_active_tests_for_cluster(project_dir, cluster)
        status, reason = validate_task3_output_strict(
            pass_n, fail_n, ign_n, expected_n, cluster.name, output,
        )
        if (status == ClusterStatus.INCOMPLETE and rc != 0
                and pass_n + fail_n + ign_n == 0):
            # A cluster whose tests did not COMPILE reports zero of everything,
            # which is indistinguishable by counts alone from a harness that
            # crashed before running anything. When cargo's own diagnostics say
            # the build failed, that is the reason to report — otherwise the
            # log blames the harness for a plain compile error.
            reason = compile_gate_reason(cluster.name, output) or reason
        floor_met = False
        verdict = (judge_cluster_binaries(
            reference, cluster.name, parse_binary_results(output))
            if status == ClusterStatus.FAIL else None)
        if verdict is not None and reference is not None:
            if verdict.met:
                floor_met = True
                status = ClusterStatus.PASS
                reason = (f"cluster '{cluster.name}': {pass_n} passed, "
                          f"{fail_n} failing test(s) the original fails too, "
                          f"{ign_n} ignored — {verdict.reason}")
                log(f"  [task3/{cluster.name}] the {fail_n} remaining "
                    f"failure(s) are failures of the ORIGINAL crate on this "
                    f"machine, not of the reconstruction — floor met.")
                emit("task3.floor_met", data={
                    "cluster": cluster.name, "reference_key": reference.key,
                    "exceeded": verdict.exceeded,
                    "advisory_unmet": sorted(f"{a}::{b}" for a, b
                                             in verdict.advisory_unmet)})
            elif verdict.advisory_unmet:
                log(f"  [task3/{cluster.name}] note: "
                    f"{len(verdict.advisory_unmet)} doc test(s) the original "
                    "passes are absent here (not gated).")
        log(f"  [task3/{cluster.name}] {status}: {reason} "
            f"(rc={rc}, {dt:.1f}s)")
        if status != ClusterStatus.PASS and deps.red_ledger is not None:
            for _line in red_ledger_lines(deps.red_ledger(project_dir, cluster.name)):
                log(f"  [task3/{cluster.name}] {_line}")
        per_cluster_results.append({
            "cluster": cluster.name,
            "status": status,
            "floor_met": floor_met,
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

    SUCCESS only when every test in every cluster genuinely runs
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
        # Primary = first ACTIONABLE non-pass cluster: one whose output carries a
        # repairable signal (failing tests, or a nonzero exit with compiler
        # diagnostics). The inline retry must rebuild under ITS toolchain/env to
        # reproduce the failure. A clean-exit shortfall (rc==0, fail==0, but
        # fewer #[test]s than statically expected) is NOT actionable — sending it
        # to the fix loop only churns (no compile error, no failing test → floor
        # metric 0). When EVERY non-pass cluster is such a shortfall we skip the
        # retry and surface a count/oracle mismatch instead of burning LLM calls.
        primary_test_output = ""
        primary_cluster = None
        _cluster_by_name = {c.name: c for c in clusters}
        _unactionable_shortfalls = [
            r for r in cluster_results if r["status"] != "pass"
            and not cluster_failure_is_actionable(r["status"], r["rc"], r["fail"])
        ]
        # Choose the primary by EVIDENCE, not by name order. `run_clusters_strict`
        # iterates name-sorted, so taking the first actionable failure picked
        # `nightly` over `stable` on 2026-08-05 because "nightly" < "stable" —
        # and `nightly` is the cluster carrying `ui`, a trybuild compile-fail
        # test no body rewrite can satisfy, while `stable` failed only
        # `test_boxed_str`.
        _actionable = [
            r for r in cluster_results
            if r["status"] != "pass" and cluster_failure_is_actionable(
                r["status"], r["rc"], r["fail"])]
        if _actionable:
            _unrepairable = not_body_repairable_tests(
                list((project_dir / "tests").rglob("*.rs")))
            _by_name = {r["cluster"]: r for r in _actionable}
            # compile-broken = INCOMPLETE with rc != 0: no verdicts, compiler
            # diagnostics present. Disjoint from every test-failing candidate
            # (those are status FAIL), so the 2026-08-05 ordering between two
            # clusters that both RAN is untouched.
            _order = rank_repair_candidates(
                [(r["cluster"], frozenset(parse_failed_tests(r["output"])),
                  r["status"] == ClusterStatus.INCOMPLETE and r["rc"] != 0)
                 for r in _actionable], _unrepairable)
            _chosen = _by_name[_order[0]]
            primary_test_output = _chosen["output"]
            primary_cluster = _cluster_by_name[_chosen["cluster"]]
            if len(_order) > 1:
                log(f"  [task3] repairing via cluster '{_order[0]}' "
                    f"(candidates: {', '.join(_order)}).")
        if primary_cluster is None and _unactionable_shortfalls:
            for r in _unactionable_shortfalls:
                log(f"  [task3/{r['cluster']}] count/oracle mismatch — clean run "
                    f"({r['pass']} pass, rc={r['rc']}) but {r['expected']} expected; "
                    "NOT a code defect, skipping the code-fix retry (would churn). "
                    "Check expected-count derivation or a genuinely missing test.")
        genesis = deps.read(genesis_path)
        deps.write(output_file,
                   f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n"
                   f"{OUTPUT_SEPARATOR}\n{aggregated_output}")

        all_pass = not any_failed and not any_incomplete
        log(f"  TASK 3 aggregate: {agg_pass}/{agg_expected} passed, "
            f"{agg_fail} failed, {agg_ignored} ignored across "
            f"{len(per_cluster_results)} cluster(s) — "
            f"{'PASS' if all_pass else ('FAIL' if any_failed else 'INCOMPLETE')}.")

        if deps.measure_agreement is not None:
            _by_cluster = {c.name: c for c in clusters}
            for _r in cluster_results:
                if _r["cluster"] in _by_cluster:
                    deps.measure_agreement(project_dir, _by_cluster[_r["cluster"]],
                                           int(_r["pass"]), int(_r["expected"]))

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
                   "reason": ("incomplete_shortfall_unactionable"
                              if _unactionable_shortfalls and primary_cluster is None
                              else "incomplete" if any_incomplete and not any_failed
                              else "failed_tests"),
                   "output": deps.store_prompt_content(aggregated_output)})

        # Inline retry on the primary failing cluster. Uses the
        # full attribution chain (panics → compile → classifier →
        # recently_filled → reverse_map) to identify fns to refix.
        if primary_test_output:
            retry_passed, retry_output = deps.task3_inline_retry(
                project_dir=project_dir,
                test_command="cargo test --no-fail-fast",
                iteration=iteration,
                initial_test_output=primary_test_output,
                cluster=primary_cluster,
                # Every baked cluster, not just the one whose failure opened the
                # retry: the tree it repairs is one artifact that all of them
                # compile, so the accept gate has to be able to see a body that
                # fixes this cluster and breaks another.
                all_clusters=clusters,
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
                r_pass, r_fail, _r_ign = parse_test_counts(r_output)
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
