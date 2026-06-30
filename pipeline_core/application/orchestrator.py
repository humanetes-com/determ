"""Orchestrator — the pipeline's composition/phase plumbing extracted from the
`main()` god-function (P5). Grown strangler-fig: pure, engine-independent pieces
cross first (CLI parser), then the phase blocks behind explicit ports as their
dependency surface shrinks. `pipeline.py:main()` stays the entrypoint and calls
into here.
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
