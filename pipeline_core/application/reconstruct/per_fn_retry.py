"""Per-fn retry use-case — extracted from _task3_inline_retry (V1 / §7.0b).

`fix_one_ref` re-asks the model for one (file, fn) body and splices it in.
`apply_per_fn_fixes` drives B2 isolation: one fn's exception is logged and
skipped, never aborting the improvement pass or masking the other fns.

Extracted from the closure nest so B2's fault-isolation behaviour is
harness-testable without monkeypatching module globals.
"""
from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.refine_loop import run_callback_loop
from pipeline_core.domain.models import FnLocator, Frontier
from pipeline_core.domain.retry_outcome import (
    RetryMode,
    RetryVerdict,
    build_verdict,
    failing_test_verdict,
)
from pipeline_core.domain.source_snapshot import SourceSnapshot
from pipeline_core.domain.test_results import (
    parse_failed_tests,
    parse_test_counts,
    strip_ansi,
)
from pipeline_core.prompts.context import PerFnFixSections


@dataclass(frozen=True, slots=True)
class RetryContext:
    """Per-run data previously closed over by _task3_inline_retry locals."""
    project_dir: Path
    src_root: Path
    cluster_label: str
    containers: dict[FnLocator, str]
    type_recs: list[Any]
    fr_path: Path | None
    tests_dir_files: list[Path]


@dataclass(frozen=True, slots=True)
class RetryDeps:
    """Collaborators injected by the composition root (pipeline.py)."""
    locate_fn: Callable[[str, str], tuple[Any, ...] | None]
    gather_fix_context: Callable[..., Any]
    build_per_fn_fix_prompt: Callable[..., str]
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    splice_fn_body: Callable[..., tuple[bool, int]]
    find_tests_for_fn: Callable[..., list[Any]]
    focus_cargo_errors: Callable[[str, str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Task3Cargo:
    """The cluster-scoped cargo seam (env builder + test runner + compile-only
    diagnostic runner)."""
    env: Callable[[], dict[str, str]]
    run: Callable[..., tuple[int, str]]
    check_diag: Callable[..., tuple[int, str, list[Any]]]


@dataclass(frozen=True, slots=True)
class Task3Attribution:
    """Failure → (file, fn) repair-target attribution (test-failure classifier,
    test reverse-map, compile-diagnostic frontier, test-oracle filter)."""
    attribute_failures: Callable[..., tuple[Frontier, str]]
    build_test_reverse_map: Callable[..., dict[str, set[FnLocator]]]
    frontier_from_diags: Callable[..., Frontier]
    filter_test_targets: Callable[..., Frontier]


@dataclass(frozen=True, slots=True)
class Task3Enumerate:
    """Cluster-aware type/container enumeration for the per-fn fix prompts."""
    cluster_filter_type_methods: Callable[..., list[Any]]
    enumerate_types: Callable[..., list[Any] | None]
    fn_container_index: Callable[..., dict[FnLocator, str]]


@dataclass(frozen=True, slots=True)
class Task3RetryDeps:
    """Composed collaborators for the task3 inline-retry orchestration. Each
    field is a narrow ISP role (cargo / attribution / enumeration / per-fn fix)
    so this is composition, not a flat god-deps."""
    cargo: Task3Cargo
    attribution: Task3Attribution
    enumerator: Task3Enumerate
    retry_fix: RetryDeps
    log: Callable[[str], None]
    test_timeout: int


def run_task3_inline_retry(
    project_dir: Path,
    test_command: str,
    iteration: Any,
    initial_test_output: str,
    cluster: Any,
    *,
    max_stalls: int,
    hard_cap: int,
    deps: Task3RetryDeps,
) -> tuple[bool, str]:
    """Improvement-driven retry over a failed task3. Returns (passed, final_output).
    If the retry cannot beat the initial failure floor, restores the source tree
    from snapshot and returns (False, initial_output).

    Extracted from pipeline.py's `_task3_inline_retry` glue (chunk H): all engine
    pieces (per-fn fix, attribution, verdicts, snapshot, improvement loop) are
    already in pipeline_core; this composes them. The composition root keeps a
    thin shim binding the resident collaborators (preserves the scripted-cargo /
    scripted-model monkeypatch seams).
    """
    _ = iteration  # signature-compat; not used by the retry policy
    # R9 residual: be CLUSTER-AWARE. task3 runs each cluster with its own toolchain
    # + env_extra (RUSTFLAGS/cfg); the failing cluster is threaded in so the retry
    # reproduces THAT cluster's build under THAT env — the global cargo env would
    # use whatever cluster is stale-set, so a cluster-cfg-specific compile error
    # (e.g. nightly-only) would not reproduce and BUILD mode would silently bail.
    _cluster_label = cluster.name if cluster is not None else "task3"
    if cluster is not None:
        _parts = ["cargo"]
        if cluster.toolchain:
            _parts.append(f"+{cluster.toolchain}")
        _parts += ["test", "--no-fail-fast"]
        test_command = shlex.join(_parts)

    def _env() -> dict[str, str]:
        # Mirror task3's per-cluster invocation: base cargo env, then the cluster's
        # env_extra overlaid (its RUSTFLAGS/cfg win over the stale global cluster's).
        e = deps.cargo.env()
        if cluster is not None and cluster.env_extra:
            e.update(cluster.env_extra)
        return e

    # R9.1: two modes. TEST mode (some test FAILED) attributes via the test
    # reverse-map/classifier. BUILD mode (zero FAILED lines because the tree did
    # not COMPILE) attributes via structured cargo diagnostics and fixes the fns
    # the compile errors point at — TASK 4 (a test-failure analyzer) cannot, since
    # it has no failing-test identifiers. compile_cmd compiles the tests without
    # running them so a 0-test "incomplete" cluster yields real error diagnostics.
    failed = sorted(parse_failed_tests(initial_test_output))
    compile_cmd = test_command if "--no-run" in test_command else test_command + " --no-run"
    mode = RetryMode.BUILD if not failed else RetryMode.TEST
    build_mode = mode is RetryMode.BUILD
    if build_mode:
        _, _, _init_diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        if not _init_diags:
            # No FAILED tests AND no reproducible compile errors under this env
            # (e.g. the failure is cluster-cfg-specific) — nothing to act on here.
            return False, initial_test_output
        initial_count = len(_init_diags)
        deps.log(f"  [task3-retry] build-failure mode: {initial_count} compile error(s).")
    else:
        initial_count = len(failed)
        deps.log(f"  [task3-retry] entering inline retry: {initial_count} failing test(s).")

    # Snapshot every .rs under src/ (the files a retry might touch; tests/ is
    # excluded by the root → cheap) so a regressing retry can be reverted.
    src_root = project_dir / "src"
    snapshot = SourceSnapshot.capture(src_root)

    # P5.1 parity: fix prompts here previously carried no TYPES_IN_SCOPE and
    # no container — the model had to guess struct fields. Enumerate from the
    # current (reconstructed) src; cluster-filter so only methods that compile
    # under THIS cluster are advertised (Hard Rule 8).
    _retry_type_recs = deps.enumerator.cluster_filter_type_methods(
        deps.enumerator.enumerate_types(src_root) or [], cluster)
    _retry_containers = deps.enumerator.fn_container_index(src_root)
    # Model-notes recall + relevant tests: resolve the module FR doc when
    # unambiguous (single-module case: exactly one functional_requirements_*.md;
    # multi-module would need a per-file module map — left as today's None).
    _fr_candidates = sorted(project_dir.glob("functional_requirements_*.md"))
    _retry_fr_path = _fr_candidates[0] if len(_fr_candidates) == 1 else None
    _retry_tests_dir_files = list((project_dir / "tests").rglob("*.rs"))

    _retry_ctx = RetryContext(
        project_dir=project_dir,
        src_root=src_root,
        cluster_label=_cluster_label,
        containers=_retry_containers,
        type_recs=_retry_type_recs,
        fr_path=_retry_fr_path,
        tests_dir_files=_retry_tests_dir_files,
    )

    def _run_check_test() -> tuple[bool, str, int]:
        rc, out = deps.cargo.run(test_command, project_dir, _env(),
                                 timeout=deps.test_timeout)
        f = parse_failed_tests(out)
        return rc == 0, out, len(f)

    def _run_check_build() -> tuple[bool, str, int]:
        rc, rendered, diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        # ok = the tree COMPILES; metric = remaining compile errors (lower better).
        return rc == 0, strip_ansi(rendered), len(diags)

    def _do_fix_test(output: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        # FR-024: full attribution chain. The classifier is one step in a
        # longer chain — when it returns no actionable refs, fall back to
        # the test-reverse-map. Previously this gave up at classifier
        # exhaustion; now it has more candidates to try.
        fails_now = sorted(parse_failed_tests(output))
        if not fails_now:
            return {"abort": True, "reason": "no_failed_tests"}
        try:
            tests_dir_files = list((project_dir / "tests").rglob("*.rs"))
            test_reverse_map = deps.attribution.build_test_reverse_map(
                tests_dir_files, project_dir)
        except Exception:
            test_reverse_map = {}
        frontier, _kind = deps.attribution.attribute_failures(
            output, project_dir,
            recently_filled=[],  # task3 has no per-cluster fill history
            test_reverse_map=test_reverse_map,
            cluster_name=_cluster_label,
        )
        frontier = deps.attribution.filter_test_targets(frontier, project_dir)
        refs: list[FnLocator] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "no_actionable_refs"}
        fixed_count = apply_per_fn_fixes(refs, output, _retry_ctx, deps.retry_fix)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    def _do_fix_build(output: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        _, _, diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        if not diags:
            return {"abort": True, "reason": "no_compile_errors"}
        frontier = deps.attribution.filter_test_targets(
            deps.attribution.frontier_from_diags(diags, project_dir), project_dir)
        refs: list[FnLocator] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "unattributed_compile_errors"}
        fixed_count = apply_per_fn_fixes(refs, output, _retry_ctx, deps.retry_fix)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    passed, last_out, history = run_callback_loop(
        name="task3.inline_retry",
        run_check=_run_check_build if build_mode else _run_check_test,
        do_fix=_do_fix_build if build_mode else _do_fix_test,
        max_stalls=max_stalls,
        hard_cap=hard_cap,
    )

    if build_mode:
        # Re-measure compile errors. Revert if the retry INCREASED them; recurse
        # (changed output) only on strict progress so task3's re-verification
        # terminates — fewer errors each round, or back to the unchanged input.
        _, _, fin_diags = deps.cargo.check_diag(
            compile_cmd, project_dir, _env(), timeout=deps.test_timeout)
        final_err = len(fin_diags)
        verdict = build_verdict(initial_errors=initial_count,
                                final_errors=final_err, has_snapshot=bool(snapshot))
        if verdict is RetryVerdict.REVERT:
            deps.log(f"  [task3-retry] build-fix worsened compile ({final_err} > "
                     f"{initial_count}) — reverting from snapshot.")
            snapshot.restore()
            return False, initial_test_output
        if verdict is RetryVerdict.RECURSE:
            deps.log(f"  [task3-retry] build-fix progress: {final_err} compile "
                     f"error(s) (initial {initial_count}) — re-running TASK 3.")
            return False, last_out
        deps.log(f"  [task3-retry] build-fix made no progress ({final_err} error(s)) "
                 "— escalating to TASK 4.")
        return False, initial_test_output

    final_failed = parse_failed_tests(last_out)
    if passed:
        deps.log(f"  [task3-retry] tests now PASS after {len(history)} iter(s).")
        return True, last_out
    # Decide whether to keep partial progress or revert. R9.2: a non-compiling tree
    # emits NO `FAILED` lines, so `len(final_failed)` reads as 0 ("near success") even
    # when a re-splice broke the build. Compile/ran-state is the real signal — if the
    # tree ran tests before the retry but no longer does, the retry made it STRICTLY
    # WORSE (a tree that won't compile is worse than N failing tests) → revert.
    init_p, init_f, _ = parse_test_counts(initial_test_output)
    fin_p, fin_f, _ = parse_test_counts(last_out)
    init_ran = (init_p + init_f) > 0
    final_ran = (fin_p + fin_f) > 0
    verdict = failing_test_verdict(
        passed=False, initial_failures=initial_count,
        final_failures=len(final_failed), initially_ran=init_ran,
        finally_ran=final_ran, has_snapshot=bool(snapshot))
    if verdict is RetryVerdict.REVERT:
        reason = ("retry broke the build (tests no longer run)"
                  if (init_ran and not final_ran)
                  else f"result {len(final_failed)} > initial {initial_count}")
        deps.log(f"  [task3-retry] {reason} — reverting from snapshot.")
        snapshot.restore()
        return False, initial_test_output
    deps.log(f"  [task3-retry] stopped at {len(final_failed)} failing tests "
             f"(initial {initial_count}). Keeping partial progress.")
    return False, last_out


def fix_one_ref(
    rel_f: str, fn: str, output: str, ctx: RetryContext, deps: RetryDeps,
) -> bool:
    """Re-ask the model for one (file, fn) body and splice it; True if spliced.

    Frozen tests/gen_tests files: return False (test oracle is immutable).
    Benign no-ops (missing file, fn not located, empty model body): False.
    Transport/model/splice failures: RAISE — the caller isolates per-fn (B2).
    """
    if rel_f.startswith("tests/") or deps.is_gen_tests_file(Path(rel_f).name):
        return False
    fp = ctx.project_dir / rel_f
    if not fp.exists():
        return False
    try:
        cur_text = fp.read_text(encoding="utf-8")
    except Exception:
        return False
    sloc = deps.locate_fn(cur_text, fn)
    if sloc is None:
        return False
    sig_t = sloc[0]
    cur_body = cur_text[sloc[2]:sloc[3]]
    err_excerpt = deps.focus_cargo_errors(output, rel_f)
    _src_rel = rel_f[4:] if rel_f.startswith("src/") else rel_f
    fix_ctx = deps.gather_fix_context(
        file_text=cur_text, file_rel=rel_f, fn_name=fn,
        sig_text=sig_t, body_text=cur_body,
        error_excerpt=err_excerpt,
        container=ctx.containers.get(FnLocator(_src_rel, fn), ""),
        type_records=ctx.type_recs,
        fr_path=ctx.fr_path,
    )
    t_blocks = deps.find_tests_for_fn(
        ctx.tests_dir_files, fn, file_rel=rel_f,
        src_dir=ctx.src_root, container=fix_ctx.container,
    )
    prompt = deps.build_per_fn_fix_prompt(
        file_text=cur_text, fn_name=fn, sig_text=sig_t,
        test_blocks=t_blocks, module_name=ctx.project_dir.name,
        cluster_name=ctx.cluster_label, file_rel=rel_f,
        prev_body=cur_body, error_excerpt=err_excerpt,
        sections=PerFnFixSections(
            container=fix_ctx.container, types=fix_ctx.types_section,
            notes=fix_ctx.notes_section,
            macros="", invariants="", retrospective_guidance="",
            siblings="", crate_idioms="",
            bound_methods=fix_ctx.bound_methods_section,
        ),
        fr_path=ctx.fr_path,
    )
    new_body, _ = deps.ask_per_fn_body(prompt, fn, [], sig_text=sig_t)
    if not new_body:
        return False
    ok, _ = deps.splice_fn_body(fp, fn, new_body, allow_nonstub=True)
    if ok:
        deps.log(f"  [task3-retry] re-spliced {rel_f}::{fn}.")
        return True
    return False


def apply_per_fn_fixes(
    refs: list[FnLocator], output: str, ctx: RetryContext, deps: RetryDeps,
) -> int:
    """Re-ask + splice each (file, fn). B2: one fn's exception is logged and
    skipped — never aborts the pass, never masks the other fixable frontier fns.
    """
    fixed_count = 0
    for rel_f, fn in refs:
        try:
            if fix_one_ref(rel_f, fn, output, ctx, deps):
                fixed_count += 1
        except Exception as e:
            deps.log(
                f"  [task3-retry] fn {rel_f}::{fn} skipped: {type(e).__name__}: {e}"
            )
    return fixed_count
