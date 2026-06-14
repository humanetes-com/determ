"""Orchestrator — the pipeline's composition/phase plumbing extracted from the
`main()` god-function (P5). Grown strangler-fig: pure, engine-independent pieces
cross first (CLI parser), then the phase blocks behind explicit ports as their
dependency surface shrinks. `pipeline.py:main()` stays the entrypoint and calls
into here.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
