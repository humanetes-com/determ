"""Orchestrator — the pipeline's composition/phase plumbing extracted from the
`main()` god-function (P5). Grown strangler-fig: pure, engine-independent pieces
cross first (CLI parser), then the phase blocks behind explicit ports as their
dependency surface shrinks. `pipeline.py:main()` stays the entrypoint and calls
into here.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from pipeline_core.application.cluster_augment import AugmentContext
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.enums import Backend
from pipeline_core.domain.generation import GenerationRequest

# The --phase-*-only / --skeleton-backend standalone subcommands, in dispatch
# order. Each maps an `args` flag attribute to a handler keyed by the same name.
_PHASE_SUBCOMMAND_FLAGS = (
    "phase_a_only", "phase_b_only", "phase_c_only", "phase_d_only", "skeleton_backend",
)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the pipeline CLI parser. Pure — no parsing, no I/O."""
    parser = argparse.ArgumentParser(description="Deterministic process pipeline.")
    parser.add_argument("--project", default="agave",
                        help="Project subfolder name (default: agave)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete all generated state and start from scratch")
    parser.add_argument("--progress", action="store_true",
                        help="Compact terminal progress bar (\\r-rewriting) instead of one line per chunk")
    parser.add_argument("--nolog", action="store_true",
                        help="Print to stdout only — do not write to run.log")
    parser.add_argument("--augment-tests", action="store_true",
                        help=(
                            "Force TASK 0c even if .coverage_augmented already exists. "
                            "Normally TASK 0c runs automatically when the sentinel is absent."
                        ))
    parser.add_argument("--repo", default=None,
                        help="Git URL or local path — cloned/copied into tmp/, git-managed")
    parser.add_argument("--best-models-list", action="store_true",
                        help="Fetch and display top open-source coding models from HuggingFace")
    parser.add_argument("--vram-gb", type=float, default=32.0,
                        help="Total GPU VRAM in GB used to annotate model fit (default: 32)")
    parser.add_argument("--from-spec", action="store_true",
                        help=(
                            "Forward (spec-driven) mode. Skip task0a/0b/0c/strip/task1. "
                            "Requires functional_requirements.md at the project root. "
                            "API surface (if present at .original_structure/{name}.api.md) "
                            "is used by TASK 2. Goes directly to module iteration loop "
                            "(TASK 2 → TASK 3 → TASK 4)."
                        ))
    parser.add_argument("--restart-from-augmented", action="store_true",
                        help=(
                            "Restore the workspace to the task0c augmentation commit "
                            "(tag: task0c-augmented-{module}) and clear only iteration "
                            "state (strip sentinels, FR file, tests_output, manifest, "
                            "failure log). Keeps task0a/0b/0c outputs. Strip + task1+ "
                            "re-run from scratch with the augmented test suite. "
                            "Single-module workspaces only for now."
                        ))
    parser.add_argument("--audit", action="store_true",
                        help=(
                            "Self-audit: run the flow auditor (pipeline_core.audit — "
                            "doc-channel / floor / plateau / no-wipe / ratchet "
                            "invariants) at every iteration boundary and every "
                            "AUDIT_INTERVAL_SEC (default 900) in the background; "
                            "verdicts go to the log ([audit] lines) and to "
                            "events.jsonl (flow_audit.report) for the monitor."
                        ))
    parser.add_argument("--phase-a-only", action="store_true",
                        help=(
                            "Standalone bake: discover clusters from live source, "
                            "run the Rust skeleton extractor per cluster, save the "
                            "skeleton + leaf-first ordering under "
                            "tmp/{project}/.skeleton/{cluster}/. Refuses to run if "
                            "strip has already happened (no source to read)."
                        ))
    parser.add_argument("--phase-b-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B: run Phase A then ask "
                            "the LLM to group the project's test files into cohesive units. "
                            "Writes the validated group manifest to "
                            "tmp/{project}/.skeleton/groups.json and reports group counts. "
                            "For testing the planner before wiring up Phase C/D."
                        ))
    parser.add_argument("--phase-c-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C: run A and B, then "
                            "fill the skeleton group-by-group (leaf-first). Filled source "
                            "lands at tmp/{project}/.skeleton-filled/. Reports bytes filled "
                            "per group. No final compile/test run."
                        ))
    parser.add_argument("--phase-d-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C + D: full skeleton "
                            "pipeline including the residual-cleanup pass. Reports stubs "
                            "filled vs left untouched."
                        ))
    parser.add_argument("--skeleton-backend", action="store_true",
                        help=(
                            "Invoke StaticSkeletonTask2Generator.generate() directly "
                            "(per-cluster Plan 4 flow). Reads functional_requirements.md "
                            "and genesis.md from the project, runs Phase A→D per cluster, "
                            "then merges into project_dir/src/. Standalone test path."
                        ))
    return parser


def run_phase_subcommand(
    args: argparse.Namespace,
    project_dir: Path,
    handlers: Mapping[str, Callable[[Path], int]],
) -> int | None:
    """If a `--phase-*-only` / `--skeleton-backend` flag is set, run its handler
    and return the process exit code; otherwise return None (normal pipeline).

    Dispatch order is fixed (`_PHASE_SUBCOMMAND_FLAGS`); each handler takes the
    project dir and returns an exit code — the routing the orchestrator owns,
    the work stays in the injected handlers.
    """
    for flag in _PHASE_SUBCOMMAND_FLAGS:
        if getattr(args, flag):
            return handlers[flag](project_dir)
    return None


@dataclass(frozen=True)
class PhaseSubcommandDeps:
    """Collaborators the standalone `--phase-*-only` / `--skeleton-backend`
    handlers need. The composition root binds these to the resident pipeline
    shims; the handlers below own only the phase-chain orchestration."""
    log: Callable[[str], None]
    bake_skeleton_artifacts: Callable[[Path, str], dict[str, Any]]
    collect_test_files: Callable[[Path], Iterable[str]]
    group_tests: Callable[..., list[dict[str, Any]]]
    fill_groups: Callable[..., dict[str, Any]]
    cleanup_residuals: Callable[..., dict[str, Any]]
    new_skeleton_generator: Callable[[], Any]
    manifest_name: str


def _load_test_pairs(collect: Callable[[Path], Iterable[str]],
                     project_dir: Path) -> list[tuple[str, str]]:
    """Read each protected test file's (rel, content); skip unreadable ones."""
    pairs: list[tuple[str, str]] = []
    for rel in sorted(collect(project_dir)):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    return pairs


def run_phase_a(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A: run the pre-strip skeleton bake on live source.

    Per Hard Rule 11, the bake MUST run while the original source is still on
    disk (before strip). Refuses to run if `src/` is missing or empty."""
    deps.log(f"\n=== Phase A (skeleton bake) for {project_dir.name} ===")
    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        deps.log(f"  [phase-a] no source at {src_dir} — strip already ran. "
                 "Cannot bake skeleton from absent source (Hard Rule 11).")
        return 2
    res = deps.bake_skeleton_artifacts(project_dir, project_dir.name)
    if not res.get("ok"):
        deps.log(f"  [phase-a] bake FAILED: {res.get('reason', 'unknown')}")
        return 3
    deps.log(f"  [phase-a] baked {len(res['clusters'])} cluster(s): "
             f"{', '.join(res['clusters'])}")
    return 0


def run_phase_b(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B: skeleton + LLM-driven test grouping."""
    rc = run_phase_a(deps, project_dir)
    if rc != 0:
        return rc
    deps.log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    if not pairs:
        deps.log("  [phase-b] no test files found.")
        return 6
    groups = deps.group_tests(skeleton_dir, pairs, cluster_name="default",
                              module_name=project_dir.name)
    deps.log(f"  [phase-b] manifest written to {skeleton_dir / 'groups.json'} "
             f"({len(groups)} groups, "
             f"{sum(len(g['test_files']) for g in groups)} test files covered).")
    for g in groups:
        deps.log(f"    - {g['name']}: {len(g['test_files'])} test(s); "
                 f"{len(g.get('target_symbols', []))} target symbol(s)")
    return 0


def run_phase_c(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B + C: skeleton → grouping → per-group fill."""
    rc = run_phase_a(deps, project_dir)
    if rc != 0:
        return rc
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    if not pairs:
        deps.log("  [phase-c] no test files found.")
        return 6

    deps.log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    groups = deps.group_tests(skeleton_dir, pairs, cluster_name="default",
                              module_name=project_dir.name)
    deps.log(f"  [phase-b] {len(groups)} groups produced.")

    deps.log(f"\n=== Phase C (per-group fill) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    if target_src_dir.exists():
        shutil.rmtree(target_src_dir)
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
    genesis = genesis_path.read_text(encoding="utf-8") if genesis_path.exists() else ""

    result = deps.fill_groups(skeleton_dir, target_src_dir, groups, project_dir,
                              cluster_name="default", module_name=project_dir.name,
                              functional_req=fr, genesis=genesis)
    deps.log(f"  [phase-c] processed {result['groups_processed']} group(s), "
             f"filled {result['bytes_filled']:,} bytes; "
             f"{len(result['errors'])} error(s).")
    for e in result["errors"]:
        deps.log(f"    - {e}")
    # Always return 0 so downstream callers (Phase D, the backend class) can
    # perform residual cleanup regardless of per-group failures.
    return 0


def run_phase_d(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone Phase A + B + C + D."""
    rc = run_phase_c(deps, project_dir)
    if rc != 0:
        return rc
    deps.log(f"\n=== Phase D (residual cleanup) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    pairs = _load_test_pairs(deps.collect_test_files, project_dir)
    fr_path = project_dir / "functional_requirements.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""

    result = deps.cleanup_residuals(target_src_dir, project_dir, fr, pairs,
                                    module_name=project_dir.name)
    deps.log(f"  [phase-d] {result['stubs_total']} residual stubs, "
             f"{result['stubs_exercised']} reachable from tests; "
             f"{result['files_updated']} file(s) updated; "
             f"{result['stubs_remaining']} stub(s) still remaining.")
    return 0


def run_skeleton_backend(deps: PhaseSubcommandDeps, project_dir: Path) -> int:
    """Standalone harness invoking the static skeleton Task2 generator directly.
    Drives the full per-cluster Plan 4 flow without task0c/strip/task1."""
    deps.log(f"\n=== Skeleton backend (Plan 4 per-cluster) for {project_dir.name} ===")
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    manifest_path = project_dir / deps.manifest_name
    if not fr_path.exists():
        # No FR yet — synthesize an empty one so the backend can still run.
        fr_path.write_text("# (no functional_requirements.md available)\n", encoding="utf-8")
    if not genesis_path.exists():
        genesis_path.write_text("# (no genesis.md available)\n", encoding="utf-8")
    backend = deps.new_skeleton_generator()
    ok = backend.generate(GenerationRequest(
        project_dir=project_dir,
        genesis_path=genesis_path,
        functional_req_path=fr_path,
        manifest_path=manifest_path,
        existing_test_files=deps.collect_test_files(project_dir),
        current_module=project_dir.name,
    ))
    return 0 if ok else 8


class DepGraphView(Protocol):
    """The slice of the dependency graph the per-module loop needs (structural —
    every `workspace_scan` graph backend satisfies it without inheritance)."""

    def crate_dir(self, module_name: str) -> Path: ...
    def crate_type(self, module_name: str) -> str: ...
    def workspace_deps(self, module_name: str) -> set[str]: ...
    def module_test_command(self, test_command: str, module_name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    """Per-module loop-invariant facts derived once at the top of each iteration."""
    module_dir: Path
    crate_type: str
    workspace_deps: set[str]
    module_test_cmd: str
    module_manifest: Path
    module_dir_rel: str


def module_descriptor(
    module_name: str,
    *,
    dep_graph: DepGraphView,
    is_multi_module: bool,
    project_dir: Path,
    test_command: str,
) -> ModuleDescriptor:
    """Resolve a module's dir/type/deps/test-cmd/manifest/rel-path. Single-module
    workspaces short-circuit to the project root + `lib` defaults (no graph)."""
    module_dir = dep_graph.crate_dir(module_name) if is_multi_module else project_dir
    crate_type = dep_graph.crate_type(module_name) if is_multi_module else "lib"
    workspace_deps = dep_graph.workspace_deps(module_name) if is_multi_module else set()
    module_test_cmd = (dep_graph.module_test_command(test_command, module_name)
                       if is_multi_module else test_command)
    module_dir_rel = (
        str(module_dir.relative_to(project_dir)) + "/"
        if is_multi_module and module_dir != project_dir else ""
    )
    return ModuleDescriptor(
        module_dir=module_dir,
        crate_type=crate_type,
        workspace_deps=workspace_deps,
        module_test_cmd=module_test_cmd,
        module_manifest=project_dir / f".generated_files_{module_name}.txt",
        module_dir_rel=module_dir_rel,
    )


_EXAMPLE_USAGE_HEADER = (
    "// !!!! TASK 1 NOTE: This file is from the project's `examples/` directory.\n"
    "// !!!! It demonstrates idiomatic API usage. It is NOT a test and contains\n"
    "// !!!! no assertions to enforce. Use it to understand HOW the public API is\n"
    "// !!!! invoked, but do not derive requirements from it unless the same\n"
    "// !!!! behavior is also asserted by a test in the test suite.\n\n"
)


def with_example_usage_signal(
    t1_input: list[tuple[str, str]], examples: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Append `examples/*.rs` files to the TASK 1 input as usage signal — each
    prefixed with a header marking it non-assertional so the model doesn't derive
    requirements from example-only behaviour. Unchanged input when no examples."""
    if not examples:
        return t1_input
    return t1_input + [(rel, _EXAMPLE_USAGE_HEADER + content) for rel, content in examples]


def next_module(
    modules: list[str], locked_modules: set[str], module_coverage: Mapping[str, float],
) -> str | None:
    """The next unprocessed module to reconstruct: the unlocked one with the
    highest coverage (ties broken by `modules` order via a stable sort). No
    topological constraint — unprocessed modules keep real source on disk, so the
    crate compiles in any order. Returns None when all modules are locked."""
    candidates = [(module_coverage.get(m, 0.0), m)
                  for m in modules if m not in locked_modules]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def measure_baseline_coverage(
    modules: list[str], dep_graph: DepGraphView, *,
    tarpaulin_available: bool,
    run_tarpaulin: Callable[[Path, str], tuple[float, Any, Any, Any]],
    project_dir: Path,
    log: Callable[[str], None],
) -> dict[str, float]:
    """Measure each module's baseline coverage once to set processing order
    (descending coverage = richer spec first). proc-macro crates report 100%
    (tarpaulin can't instrument them). Returns {} when tarpaulin is unavailable
    or there is only one module."""
    result: dict[str, float] = {}
    if not tarpaulin_available or len(modules) <= 1:
        return result
    log("\nMeasuring baseline coverage to determine processing order...")
    for mod_name in modules:
        crate_type = dep_graph.crate_type(mod_name)
        if "proc-macro" in crate_type.lower():
            cov = 100.0
        else:
            cov, _, _, _ = run_tarpaulin(project_dir, mod_name)
        log(f"  {mod_name}: {cov:.1f}%")
        result[mod_name] = cov
    return result


@dataclass(frozen=True)
class LockDeps:
    """Collaborators for the lock/complete transition (per-block injection)."""
    commit_and_mark: Callable[..., None]
    set_current: Callable[[Path, str], None]
    lock_module: Callable[[Path, str, Path, Path], object]  # returns int; ignored
    git_commit: Callable[[Path, str], bool]
    log: Callable[[str], None]
    sys_exit: Callable[[int], None]
    scan_unimplemented_functions: Callable[..., Any]


def lock_or_complete(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    module_manifest: Path,
    *,
    is_multi_module: bool,
    locked_modules: set[str],
    src_hash: str | None,
    commit_message: str,
    detail: str,
    set_current_label: str,
    locked_commit_msg: str,
    success_log: str,
    tag: str | None = None,
    complete_commit_msg: str | None = None,
    deps: LockDeps,
) -> None:
    """Commit+mark a passing module, then either LOCK it (multi-module: record
    in `locked_modules`, commit the lock) or COMPLETE the run (single-module:
    optionally commit the completion, then exit 0). Single-module mode does not
    return — `deps.sys_exit(0)` raises. The three call sites (t3 pass, no_files
    build-pass, fix-loop reverified pass) differ only in the message/label/tag
    arguments; the skeleton is identical."""
    deps.commit_and_mark(
        project_dir, f"task2-4/{module_name}", "done",
        commit_message=commit_message, tag=tag, detail=detail, source_hash=src_hash,
    )
    deps.set_current(project_dir, set_current_label)
    if is_multi_module:
        deps.lock_module(project_dir, module_name, module_dir, module_manifest)
        locked_modules.add(module_name)
        deps.git_commit(project_dir, locked_commit_msg)
    else:
        # Same cfg-blind stub scan as the multi-module final check in
        # `run_pipeline` — single-module projects (the common case: anyhow,
        # hardcrate, most real crates) complete HERE, not there, so without
        # this the whole-tree check would never run for them.
        orphaned_stubs = deps.scan_unimplemented_functions(project_dir)
        if orphaned_stubs:
            deps.log(f"\nWARNING: all tests passed, but {len(orphaned_stubs)} "
                      "fn(s) are STILL unimplemented!() — unreachable from "
                      "every cfg cluster that got built/tested this run:")
            for loc in orphaned_stubs:
                deps.log(f"    {loc.file}::{loc.fn_name}")
            deps.log("         This is NOT a clean success — the fns above "
                      "were never attempted.")
            if complete_commit_msg is not None:
                deps.git_commit(project_dir, complete_commit_msg)
            deps.sys_exit(1)
        deps.log(success_log)
        if complete_commit_msg is not None:
            deps.git_commit(project_dir, complete_commit_msg)
        deps.sys_exit(0)


def filter_module_test_files(
    all_test_files: list[tuple[str, str]], module_dir_rel: str,
) -> list[tuple[str, str]]:
    """This module's `(rel_path, content)` test files: all of them filtered to the
    module's dir prefix, or all of them for a single-module/root workspace
    (``module_dir_rel == ""``)."""
    if not module_dir_rel:
        return all_test_files
    return [(rel, content) for rel, content in all_test_files
            if rel.startswith(module_dir_rel)]


@dataclass(frozen=True)
class RunDeps:
    """Every collaborator the top-level pipeline run (`run_pipeline`) drives.
    The composition root (pipeline.py:main) binds these to the resident shims +
    constants + run-scoped singletons; the orchestration sequence lives here."""
    # constants
    WORKSPACE_DIR: str
    MANIFEST_NAME: str
    MODEL: str
    COVERAGE_BACKEND: Any
    MODEL_CONTEXT_CHARS: int
    SINGLE_CALL_CHAR_LIMIT: int
    FILE_CHAR_LIMIT: int
    INTERMEDIATE_CHAR_LIMIT: int
    COVERAGE_AUGMENTATION_TARGET: float
    TARPAULIN_BIN: Path
    FAILURE_LOG_NAME: str
    PROJECT_PROFILE_NAME: str
    PROJECT_CONTEXT_NAME: str
    COVERAGE_STRATEGY_NAME: str
    SETTINGS: Any
    # run-scoped singletons / deps bundles
    run_state: Any
    scope: Any
    augment_store: Any
    cluster_augmentor: Any
    strip_bake_deps: Any
    ws_scan_deps: Any
    phase_subcommand_deps: Any
    requirements_phase: Any
    module_reconstructor: Any
    # resident functions
    log: Callable[..., None]
    emit: Callable[..., None]
    setup_workspace: Callable[..., Path]
    is_git_repo: Callable[..., bool]
    init_log: Callable[..., None]
    init_events: Callable[..., None]
    start_audit_thread: Callable[..., None]
    quarantine_broken_gen_tests: Callable[..., Any]
    git_file_hash: Callable[..., str]
    ensure_vllm_ready: Callable[..., None]
    git_clean_reset: Callable[..., None]
    clean_state: Callable[..., None]
    state_path: Callable[..., Path]
    restart_from_augmented: Callable[..., None]
    source_hash: Callable[..., str]
    log_state_summary: Callable[..., None]
    read: Callable[..., str]
    task0_cfg_scan: Callable[..., Any]
    mark_phase: Callable[..., None]
    phase_valid: Callable[..., bool]
    read_state: Callable[..., Any]
    discover_test_command: Callable[..., str]
    parse_cmd: Callable[..., list[str]]
    collect_test_files: Callable[..., Any]
    collect_protected_test_files: Callable[..., Any]
    task0_project_profile: Callable[..., str]
    task0_project_context: Callable[..., str]
    task0b_discovery: Callable[..., str]
    commit_and_mark: Callable[..., None]
    dependency_graph_factory: Callable[..., Any]
    load_locked_modules: Callable[..., Any]
    run_tarpaulin: Callable[..., Any]
    strip_and_bake_module: Callable[..., None]
    task1_static_audit: Callable[..., Any]
    scan_workspace_structure: Callable[..., Any]
    infer_build_check_command: Callable[..., str]
    wl_save_gen_tests_siblings: Callable[..., Any]
    gen_tests_siblings: Callable[..., Any]
    is_gen_tests_file: Callable[..., bool]
    wl_wipe_module_sources: Callable[..., Any]
    write_workspace_stub: Callable[..., Any]
    collect_examples: Callable[..., Any]
    task3: Callable[..., Any]
    scan_unimplemented_functions: Callable[..., Any]


def run_pipeline(deps: RunDeps, args: argparse.Namespace, root: Path) -> None:
    """The deterministic per-module reconstruction run, extracted verbatim from
    pipeline.py:main (S27). `main` now only parses args, resolves `root`, builds
    `RunDeps`, and calls here."""
    workspace_dir = root / deps.WORKSPACE_DIR

    if args.repo:
        # Full git-managed workspace: clone/copy into tmp/, checkout pipeline branch.
        project_dir = deps.setup_workspace(args.repo, workspace_dir,
                                           force_fresh=args.clean)
        deps.run_state.git_managed = True
    else:
        # --project: check tmp/ first, then root (backward compat).
        candidate = workspace_dir / args.project
        project_dir = candidate if candidate.exists() else root / args.project
        deps.run_state.git_managed = deps.is_git_repo(project_dir)

    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / deps.MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    deps.init_log(root, nolog=args.nolog, progress_mode=args.progress)
    deps.init_events(product=project_dir.name)

    # --audit: periodic + iteration-boundary flow self-audit (read-only).
    deps.run_state.audit_enabled = bool(args.audit)
    deps.run_state.audit_project_dir = project_dir
    if deps.run_state.audit_enabled:
        deps.start_audit_thread()
        deps.log(f"  --audit: flow self-audit active (every iteration + every "
                 f"{os.getenv('AUDIT_INTERVAL_SEC', '900')}s).")

    # P3.13: scan gen_tests siblings inherited from prior runs; quarantine
    # any with unbalanced delimiters and strip their decls from the parent
    # source. Prevents a broken file (an LLM-corrupted task0c output) from
    # blocking every future cargo invocation. The systemic guard at write
    # time lives in _write_gen_tests_pass.
    try:
        deps.quarantine_broken_gen_tests(project_dir)
    except Exception as _e:
        deps.log(f"  [gen_tests] startup quarantine scan failed: {_e}")

    _sub_rc = run_phase_subcommand(args, project_dir, {
        "phase_a_only": lambda pd: run_phase_a(deps.phase_subcommand_deps, pd),
        "phase_b_only": lambda pd: run_phase_b(deps.phase_subcommand_deps, pd),
        "phase_c_only": lambda pd: run_phase_c(deps.phase_subcommand_deps, pd),
        "phase_d_only": lambda pd: run_phase_d(deps.phase_subcommand_deps, pd),
        "skeleton_backend": lambda pd: run_skeleton_backend(deps.phase_subcommand_deps, pd),
    })
    if _sub_rc is not None:
        sys.exit(_sub_rc)

    # Forward (spec-driven) mode: skip reconstruction-phase tasks. The user
    # provides functional_requirements.md (+ optional API surface) and tests;
    # the pipeline goes straight to TASK 2 → TASK 3 → TASK 4.
    _from_spec = args.from_spec
    if _from_spec:
        _fr_root = project_dir / "functional_requirements.md"
        if not _fr_root.exists():
            print(f"ERROR: --from-spec requires {_fr_root} (project-level FR file).")
            sys.exit(1)
        deps.log(f"FORWARD MODE: spec-driven generation. "
                 f"Skipping task0a/0b/0c/strip/task1. FR={_fr_root}")

    deps.emit("pipeline.start", data={
        "project_dir": str(project_dir),
        "clean": args.clean,
        "augment_tests": args.augment_tests,
        "from_spec": _from_spec,
        "model": deps.MODEL,
        "backend": deps.SETTINGS.backend,
        "coverage_backend": deps.COVERAGE_BACKEND,
        "context_chars": deps.MODEL_CONTEXT_CHARS,
    })

    _ph = deps.git_file_hash(root, "pipeline.py")
    _mh = deps.git_file_hash(root, "monitor.py")
    _params = []
    if args.repo:
        _params.append(f"--repo {args.repo}")
    if args.project and not args.repo:
        _params.append(f"--project {args.project}")
    if args.clean:
        _params.append("--clean")
    if args.augment_tests:
        _params.append("--augment-tests")
    if args.progress:
        _params.append("--progress")
    if args.nolog:
        _params.append("--nolog")
    if args.from_spec:
        _params.append("--from-spec")
    if args.restart_from_augmented:
        _params.append("--restart-from-augmented")
    deps.log(f"  pipeline.py [{_ph}]  monitor.py [{_mh}]  params: {' '.join(_params) or '(none)'}")

    _backend = deps.SETTINGS.backend
    if _backend is Backend.VLLM:
        provider = f"vLLM ({os.getenv('VLLM_MODEL_NAME', deps.MODEL)})"
    elif _backend is Backend.SGLANG:
        provider = f"SGLang ({os.getenv('SGLANG_MODEL_NAME', deps.MODEL)})"
    elif _backend is Backend.HUGGINGFACE:
        provider = f"HuggingFace ({os.getenv('HF_MODEL', deps.MODEL)})"
    else:
        provider = f"Ollama ({deps.MODEL})"
    deps.log(f"Using model: {deps.MODEL}  |  Provider: {provider}")
    deps.log(f"Context budget: {deps.MODEL_CONTEXT_CHARS:,} chars  "
             f"(SINGLE_CALL={deps.SINGLE_CALL_CHAR_LIMIT:,}  FILE={deps.FILE_CHAR_LIMIT:,}  "
             f"INTERMEDIATE={deps.INTERMEDIATE_CHAR_LIMIT:,})")

    if _backend is Backend.VLLM:
        deps.ensure_vllm_ready(root, deps.log)

    if args.clean and args.restart_from_augmented:
        deps.log("\n-- ERROR: --clean and --restart-from-augmented are mutually exclusive --")
        sys.exit(2)

    if args.clean:
        deps.log("\n-- --clean requested: wiping all generated state --")
        if deps.run_state.git_managed:
            deps.git_clean_reset(project_dir)
        deps.clean_state(root, project_dir, manifest_path, genesis_path, functional_req_path)
        deps.state_path(project_dir).unlink(missing_ok=True)

    if args.restart_from_augmented:
        deps.log("\n-- --restart-from-augmented: restoring workspace to task0c augmented state --")
        try:
            deps.restart_from_augmented(root, project_dir)
        except Exception as e:
            deps.log(f"  [ABORT] restart-from-augmented failed: {e}")
            sys.exit(2)

    _src_hash = deps.source_hash(project_dir)
    deps.log(f"  Source hash: {_src_hash}")
    deps.log_state_summary(project_dir, _src_hash)

    readme_path = project_dir / "README.md"
    if readme_path.exists():
        readme_text = deps.read(readme_path)
    else:
        deps.log("WARNING: README.md not found -- proceeding without it.")
        readme_text = ""

    # Cluster scan is pure static analysis (no LLM) — run first so all downstream
    # LLM tasks (discover_test_command, task0b) have cluster context available.
    _clusters = deps.task0_cfg_scan(project_dir)
    deps.scope.reset(_clusters)
    deps.log(f"\nCompiler clusters identified: {len(_clusters)}")
    for _c in _clusters:
        _env_desc = ", ".join(f"{k}+={v}" for k, v in _c.env_extra.items()) if _c.env_extra else "default build"
        deps.log(f"  cluster/{_c.name}  — {_env_desc}")
    deps.mark_phase(project_dir, "cfg_scan", "done",
                    detail=f"{len(_clusters)} clusters: {', '.join(c.name for c in _clusters)}",
                    source_hash=_src_hash)

    if deps.phase_valid(project_dir, "test_cmd"):
        test_command = deps.read_state(project_dir)["phases"]["test_cmd"].get("detail", "")
        deps.log(f"Test command: {test_command} (from pipeline state)")
    else:
        test_command = deps.discover_test_command(readme_text, project_dir=project_dir)
        deps.mark_phase(project_dir, "test_cmd", "done", detail=test_command, source_hash=_src_hash)

    # Normalize: strip leading ./ from the executable so we use the PATH binary.
    parts_check = deps.parse_cmd(test_command)
    if parts_check and parts_check[0].startswith("./"):
        parts_check[0] = parts_check[0][2:]
        test_command = " ".join(parts_check)
        deps.log(f"  Normalized test command: {test_command}")

    deps.log("Collecting test files...")
    test_files = deps.collect_test_files(project_dir)
    existing_test_files = deps.collect_protected_test_files(project_dir)
    deps.log(f"  Found {len(test_files)} test file(s) ({len(existing_test_files)} write-protected).")

    # TASK 0a: identify project structure once; profile stored per-project (not pipeline root)
    # so running project B never loads project A's cached profile.
    # FORWARD MODE: task0a/0b/0c provide reconstruction-phase context (profile/
    # behavioral summary/coverage strategy). In spec-driven mode the user supplies
    # FR+API directly so this preprocessing is unnecessary — empty strings keep
    # downstream prompts well-formed without triggering a regeneration.
    if _from_spec:
        project_profile = ""
        project_context = ""
        coverage_strategy = ""
        deps.log("  [from-spec] Skipping task0a/0a-context/0b (FR+API are user-provided).")
    else:
        profile_path = project_dir / deps.PROJECT_PROFILE_NAME
        project_profile = deps.task0_project_profile(profile_path, test_files, readme_text)

        # TASK 0a (context): synthesize behavioral project summary from test content.
        # This is the "big picture" document injected into every task — especially TASK 0c
        # which currently receives no project-level context. Works even without README.
        context_path = project_dir / deps.PROJECT_CONTEXT_NAME
        deps.emit("task.start", task="task0a")  # debt#7: structured stage event
        project_context = deps.task0_project_context(context_path, test_files, readme_text, project_profile)

        # TASK 0b (discovery): produce coverage_strategy.md — import idioms, type construction
        # patterns, feature flags, test conventions. Injected into all TASK 0c, 1, and 2 prompts.
        # Skip if either pipeline_state marks task0b done, or the artifact already
        # exists from a prior run that predates state tracking (auto-backfill).
        strategy_path = project_dir / deps.COVERAGE_STRATEGY_NAME
        deps.emit("task.start", task="task0b")  # debt#7: structured stage event
        if deps.phase_valid(project_dir, "task0b"):
            coverage_strategy = strategy_path.read_text().strip() if strategy_path.exists() else ""
            deps.log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        elif strategy_path.exists() and strategy_path.read_text().strip():
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            coverage_strategy = strategy_path.read_text().strip()
            deps.mark_phase(project_dir, "task0b", "done",
                            detail=f"{len(coverage_strategy)} chars (existing artifact)")
            deps.log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        else:
            coverage_strategy = deps.task0b_discovery(
                strategy_path, project_dir, test_files, readme_text, project_profile,
                clusters=_clusters,
            )
            deps.commit_and_mark(
                project_dir, "task0b", "done",
                commit_message="pipeline: task0b done",
                detail=f"{len(coverage_strategy)} chars",
            )

    # TASK 0b: build dependency graph for bottom-up module integration.
    # Implementation selected by DEPENDENCY_GRAPH in .env (default: static).
    dep_graph = deps.dependency_graph_factory(project_dir, deps.ws_scan_deps)
    modules = dep_graph.topological_order()
    is_multi_module = len(modules) > 1

    if is_multi_module:
        deps.log(f"\nDependency graph: {len(modules)} module(s) in topological order:")
        for m in modules:
            module_deps = dep_graph.workspace_deps(m)
            dep_str = f"  deps: {', '.join(sorted(module_deps))}" if module_deps else "  leaf"
            deps.log(f"  [{dep_graph.crate_type(m):10s}]  {m}  ({dep_str})")
    else:
        deps.log("  Single module or no dependency graph detected — whole-workspace mode.")

    # -----------------------------------------------------------------------
    # Per-module pipeline.
    # For each module in coverage-descending order (topology respected):
    #   0c. Augment THIS module's tests until 100% coverage (real source present)
    #   Strip. Remove ONLY this module's source — all others remain on disk
    #   1.  Extract requirements from THIS module's tests
    #   2-4. Reconstruct loop until tests pass
    #   Lock. Only when passed > 0 and failed == 0
    # -----------------------------------------------------------------------
    if not modules:
        modules = [project_dir.name]

    locked_modules: set[str] = set()
    if is_multi_module:
        locked_modules = deps.load_locked_modules(project_dir)
        if locked_modules:
            deps.log(f"\nAlready locked: {', '.join(sorted(locked_modules))}")

    failure_log_path = project_dir / deps.FAILURE_LOG_NAME

    # Measure baseline coverage once to determine processing order.
    # Coverage-first: richest test spec → easiest reconstruction → best context for later modules.
    module_coverage = measure_baseline_coverage(
        modules, dep_graph, tarpaulin_available=deps.TARPAULIN_BIN.exists(),
        run_tarpaulin=deps.run_tarpaulin, project_dir=project_dir, log=deps.log)

    if is_multi_module and module_coverage:
        deps.log("\nProcessing order (coverage-descending, topology respected):")
        for m in modules:
            deps.log(f"  {m}: {module_coverage.get(m, 0.0):.1f}%")

    # -----------------------------------------------------------------------
    # TASK 0c — compiler cluster phase
    # _clusters and _current_cluster already set above (moved before task0b so
    # all LLM tasks receive cluster context). Loop below runs one pass per cluster.
    # -----------------------------------------------------------------------

    if _from_spec:
        deps.log("  [from-spec] Skipping task0c cluster loop (no test augmentation needed).")
        _clusters = []  # ensure the loop below is a no-op
    # TASK 0c cluster augmentation (R2.2 — application.cluster_augment.ClusterAugmentor).
    # Per-module crate dir/type resolved up front; _from_spec leaves _clusters empty so
    # the use-case is a no-op (it still clears the active cluster on exit).
    _module_dirs = {m: (dep_graph.crate_dir(m) if is_multi_module else project_dir)
                    for m in modules}
    _module_types = {m: (dep_graph.crate_type(m) if is_multi_module else "lib")
                     for m in modules}
    deps.cluster_augmentor.run(AugmentContext(
        project_dir=project_dir, clusters=_clusters, modules=modules,
        module_coverage=module_coverage, module_dirs=_module_dirs,
        module_types=_module_types, src_hash=_src_hash,
        project_context=project_context, coverage_strategy=coverage_strategy,
        target_pct=deps.COVERAGE_AUGMENTATION_TARGET, tarpaulin_bin=deps.TARPAULIN_BIN,
    ))

    total_iters = 0
    # Plateaued (non-converged) modules never lock — without this exclusion the
    # loop re-picks them forever with a fresh IterationState (plateau counter
    # resets each re-entry → unbounded macro-loop; observed on anyhow run 30425).
    nonconverged_modules: set[str] = set()
    while True:
        module_name = (next_module(modules, locked_modules | nonconverged_modules,
                                   module_coverage)
                       if is_multi_module else (modules[0] if modules else None))
        if module_name is None or module_name in nonconverged_modules:
            break

        _desc = module_descriptor(
            module_name, dep_graph=dep_graph, is_multi_module=is_multi_module,
            project_dir=project_dir, test_command=test_command)
        module_dir = _desc.module_dir
        crate_type = _desc.crate_type
        workspace_deps = _desc.workspace_deps
        module_test_cmd = _desc.module_test_cmd
        module_manifest = _desc.module_manifest
        module_dir_rel = _desc.module_dir_rel

        deps.log(f"\n{'=' * 60}")
        deps.log(f"MODULE: {module_name}  [{crate_type}]")
        ready_deps = workspace_deps & locked_modules
        if ready_deps:
            deps.log(f"  Locked deps  : {', '.join(sorted(ready_deps))}")
        deps.log(f"{'=' * 60}")

        # --- TASK 0c: all clusters complete — sentinel check only ---
        # All coverage augmentation (across all clusters) was done by the cluster
        # outer loop above. This block just verifies the sentinel and proceeds.
        # FORWARD MODE: skip — there is no augmentation phase in spec-driven mode.
        if not _from_spec:
            if not deps.augment_store.is_module_augmented(project_dir, module_name):
                deps.log(f"  WARNING: '{module_name}' has no augmentation sentinel — proceeding anyway.")
            deps.log(f"  TASK 0c: '{module_name}' all clusters done — proceeding to strip.")

        # Coverage check before strip — warn if below 100% but proceed anyway.
        # A hard stop here would abort the pipeline on coverage tool failures (bad sentinel,
        # grcov error, etc.) even when the test suite is intact.
        if crate_type != "proc-macro" and deps.TARPAULIN_BIN.exists():
            _final_cov = deps.augment_store.module_augmented_value(project_dir, module_name)
            if _final_cov < deps.COVERAGE_AUGMENTATION_TARGET:
                deps.log(
                    f"\n  WARNING: '{module_name}' coverage is {_final_cov:.1f}% — "
                    f"proceeding to strip anyway. Reconstruction quality may be lower."
                )

        # --- Collect test files BEFORE stripping ---
        # Must happen before strip so we can accurately identify which tests belong
        # to this module. Strip removes implementation source but test content in
        # lib.rs would also be gone if we checked after.
        test_files_pre_strip = deps.collect_test_files(project_dir)
        module_test_files = filter_module_test_files(
            test_files_pre_strip, module_dir_rel)
        module_has_tests = len(module_test_files) > 0
        deps.log(f"  Module test files: {len(module_test_files)} (before strip)")

        # Strip this module's source after baking Hard-Rule-11 artefacts
        # (P5-S3 — extracted to application/strip_bake.py).
        deps.strip_and_bake_module(
            project_dir, module_name, module_dir, crate_type=crate_type,
            from_spec=_from_spec, src_hash=_src_hash, deps=deps.strip_bake_deps,
        )

        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = deps.collect_test_files(project_dir)
        existing_test_files = deps.collect_protected_test_files(project_dir)
        deps.log(f"  Test files after strip: {len(test_files)} readable, "
                 f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = project_dir / f"functional_requirements_{module_name}.md"

        # Initialize iteration counters here so task1's task4 escalation path can use them.
        module_iter = 1
        prev_pass_count = 0
        prev_test_output = ""

        t1_input = module_test_files if module_has_tests else test_files
        if not module_has_tests:
            deps.log(f"  '{module_name}' has 0 tests — using full project tests for TASK 1.")

        # Append example files (examples/*.rs) as usage signal. They are NOT
        # assertions — the inline header makes that explicit so the LLM doesn't
        # derive requirements from example-only behavior.
        _t1_examples = deps.collect_examples(project_dir, module_dir=module_dir if is_multi_module else None)
        t1_input = with_example_usage_signal(t1_input, _t1_examples)
        if _t1_examples:
            deps.log(f"  Appended {len(_t1_examples)} example file(s) to TASK 1 input as usage signal.")

        # FORWARD MODE: copy the user-provided FR into the per-module slot so
        # TASK 2 picks it up via the existing module_req_path. Skip the full
        # TASK 1 inference loop — the user is the source of truth.
        if _from_spec:
            _fr_root = project_dir / "functional_requirements.md"
            if not module_req_path.exists():
                module_req_path.write_text(_fr_root.read_text(encoding="utf-8"),
                                           encoding="utf-8")
                deps.log(f"  [from-spec] Seeded {module_req_path.name} "
                         f"from {_fr_root.name} ({_fr_root.stat().st_size} chars).")
            else:
                deps.log(f"  [from-spec] {module_req_path.name} already present — leaving as-is.")

        # TASK 1: generate → validate → retry → escalate (P5 — RequirementsPhase use-case).
        t1_valid = deps.requirements_phase.run(
            project_dir, module_name=module_name, module_req_path=module_req_path,
            genesis_path=genesis_path, failure_log_path=failure_log_path,
            t1_input=t1_input, readme_text=readme_text, project_profile=project_profile,
            project_context=project_context, coverage_strategy=coverage_strategy,
            crate_type=crate_type, from_spec=_from_spec, src_hash=_src_hash,
            iteration=module_iter, prev_pass_count=prev_pass_count,
            prev_test_output=prev_test_output,
        )

        if not t1_valid:
            continue

        deps.task1_static_audit(module_req_path, module_test_files or test_files)

        # Workspace scan (done per-module since strip changes the file layout).
        workspace_structure = deps.scan_workspace_structure(project_dir)
        build_check_cmd = deps.infer_build_check_command(test_command, project_dir)
        module_build_check = (
            dep_graph.module_build_check_command(build_check_cmd, module_name)
            if is_multi_module else build_check_cmd
        )

        # Slice 9: module-start lifecycle (save siblings → maybe-wipe → stub)
        # lives in application/workspace_lifecycle.py; the wipe is gated by
        # the codified wipe⟺rerun invariant (session-59 t2c→t3 fall-through).
        saved_gen_blocks: dict[str, list[tuple[int, str]]] = \
            deps.wl_save_gen_tests_siblings(
                module_dir, project_dir, existing_test_files,
                gen_tests_siblings=deps.gen_tests_siblings,
                is_gen_tests_file=deps.is_gen_tests_file)
        deps.wl_wipe_module_sources(
            module_dir, project_dir, existing_test_files,
            regenerating_phase_done=deps.phase_valid(
                project_dir, f"task2b/{module_name}"),
            write_stub=partial(deps.write_workspace_stub, crate_type, module_dir))

        if not module_has_tests:
            deps.log(f"  '{module_name}' has 0 test files — will use whole-project context in TASK 2.")

        # --- TASK 2-4: reconstruction loop (P5 — ModuleReconstructor use-case). ---
        run_ctx = RunContext(
            project_dir=project_dir, module_name=module_name, module_dir=module_dir,
            crate_type=crate_type, module_manifest=module_manifest,
            module_dir_rel=module_dir_rel, module_test_cmd=module_test_cmd,
            module_build_check=module_build_check, module_has_tests=module_has_tests,
            module_req_path=module_req_path, genesis_path=genesis_path,
            failure_log_path=failure_log_path, is_multi_module=is_multi_module,
            src_hash=_src_hash, workspace_deps=workspace_deps, locked_modules=locked_modules,
            workspace_structure=workspace_structure, project_profile=project_profile,
            project_context=project_context, readme_text=readme_text,
            test_files=test_files, existing_test_files=existing_test_files,
            clusters=_clusters, saved_gen_blocks=saved_gen_blocks,
        )
        result = deps.module_reconstructor.run(
            run_ctx, total_iters=total_iters, coverage_strategy=coverage_strategy)
        total_iters, coverage_strategy = result.total_iters, result.coverage_strategy
        if not result.converged:
            nonconverged_modules.add(module_name)

    # -----------------------------------------------------------------------
    # Final: run full test suite to validate the integrated workspace.
    # Reached in multi-module mode (single-module exits on pass above) and by
    # any run with a plateaued module — that run reports FAIL and exits 1.
    # -----------------------------------------------------------------------
    if nonconverged_modules:
        deps.log(f"\nNON-CONVERGED (plateau): {', '.join(sorted(nonconverged_modules))}"
                 " — repair stopped at the plateau cap; best tree kept.")
    deps.log(f"\n{'=' * 60}")
    deps.log("FINAL: Running full test suite to validate integrated workspace")
    deps.log(f"{'=' * 60}")
    passed, _ = deps.task3(project_dir, test_command, "final", project_dir, genesis_path)
    if passed:
        # A full-suite pass only proves the ACTIVE cfg cluster's view compiles
        # and tests clean. Code gated behind a cfg predicate whose cluster got
        # dropped (too few distinguishing items to justify its own pass) is
        # invisible to every remaining cluster's build, so its stubs never
        # surface as a compile or test failure. This scan is CFG-BLIND — it
        # reads every .rs file's raw fn bodies, not just what the current
        # cluster compiles — and is the only thing that can still catch them
        # before claiming success.
        orphaned_stubs = deps.scan_unimplemented_functions(project_dir)
        if orphaned_stubs:
            deps.log(f"\nWARNING: all tests passed, but {len(orphaned_stubs)} "
                      "fn(s) are STILL unimplemented!() — unreachable from "
                      "every cfg cluster that got built/tested this run:")
            for loc in orphaned_stubs:
                deps.log(f"    {loc.file}::{loc.fn_name}")
            deps.log("         This is NOT a clean success — the fns above "
                      "were never attempted.")
            sys.exit(1)
        deps.log("\nSUCCESS: all tests passed.")
        sys.exit(0)
    elif nonconverged_modules:
        deps.log("\nFAIL: pipeline stopped — non-converged module(s) plateaued; "
                 "final suite failing. Best tree kept for inspection.")
        sys.exit(1)
    else:
        deps.log("\nWARNING: all modules locked individually but full suite failed.")
        deps.log("         Integration issues between modules may require attention.")
        sys.exit(1)
