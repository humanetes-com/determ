"""Workspace layout vocabulary — the well-known directory names the pipeline
writes under a project workspace, plus the pure rules over pipeline_state
phase keys and git tag names that the workspace restores decide with. Stdlib
only; the single source of truth so adapters and the composition root agree on
the on-disk layout instead of each repeating a string literal.

(``ORIGINAL_STRUCTURE_DIR`` currently lives in ``adapters/strip.py`` for
historical reasons; consolidate it here in a later slice.)"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Per-module locked source + the `locked.txt` ledger live here once a module's
# tests pass and it is frozen as a dependency for later modules.
LOCKED_MODULES_DIR = ".locked_modules"

# TASK 0c augmentation sentinels: one file per (cluster, module) and one per
# module, each holding the bare numeric coverage reached (no '%' text).
CLUSTER_AUGMENTED_DIR = ".cluster_augmented"
MODULE_AUGMENTED_DIR = ".module_augmented"

# Strip sentinel dir: present once a module's source has been skeletonised.
MODULE_STRIPPED_DIR = ".module_stripped"

# The three directories that hold the reconstruction itself. `src/` in the
# workspace is a stub shell after strip (a one-line lib.rs plus the
# `*.gen_tests_*.rs` oracle); what the loop builds, tests and scores lives here.
#   .skeleton/<cluster>/src        — the authored layout the bodies answer to
#   .skeleton-filled/<cluster>/src — the filled bodies; run_phase_c_tdd's
#                                    target_src_dir, and what a resume picks up
#   .best_tree/<cluster>/src       — heal's durable restore tier
# All three are TRACKED in the workspace repo (its .gitignore is /target/ and
# /Cargo.lock only), which is what makes the task2a checkpoint commits real and
# what makes a `git reset --hard` to a pre-fill tag delete them wholesale.
SKELETON_DIR = ".skeleton"
SKELETON_FILLED_DIR = ".skeleton-filled"
BEST_TREE_DIR = ".best_tree"

# Ordered widest-first: the layout and the bodies are a PAIR (t1.5 re-authors
# the layout per run, so bodies from one run do not compile against another's
# skeleton) and any operation that moves one must move all three together.
RECONSTRUCTION_DIRS: tuple[str, ...] = (
    SKELETON_FILLED_DIR, SKELETON_DIR, BEST_TREE_DIR,
)

# Per-cluster build configuration dump (env_extra / cfg / tarpaulin excludes).
BUILD_CONFIGS_NAME = "build_configs.md"

# Phase C test-fix iteration archives (per-iteration cargo output) + the
# per-fn retrospectives store. Single source of truth for the dir name.
PHASE_C_LOGS_DIR = ".phase_c_logs"

# File extensions the model is allowed to create/overwrite — anything else on
# disk (Cargo.toml, *.json, *.proto, …) is a protected non-source file. The
# wide language set keeps the guard generic across non-Rust projects.
SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    # Systems / compiled
    ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".go", ".swift", ".kt", ".kts", ".java", ".cs", ".vb", ".fs", ".fsx",
    ".d", ".nim", ".cr", ".zig", ".v", ".odin",
    # Scripting
    ".py", ".rb", ".pl", ".pm", ".php", ".php3", ".php4", ".php5", ".phtml",
    ".lua", ".sh", ".bash", ".zsh", ".fish", ".tcl",
    # JVM / functional
    ".scala", ".sc", ".clj", ".cljs", ".cljc", ".groovy", ".gvy", ".gradle",
    ".ex", ".exs", ".hs", ".lhs", ".ml", ".mli", ".mll", ".mly",
    ".erl", ".hrl", ".elm", ".purs",
    # Web / JS
    ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
    ".coffee", ".vue", ".svelte",
    # Database / query
    ".sql",
    # Other
    ".r", ".R", ".m", ".mm", ".f", ".f90", ".f95", ".f03", ".f08",
    ".ada", ".adb", ".ads", ".pas", ".pp",
})

# Failure log: running record of every iteration's outcome (fed into TASK 4).
FAILURE_LOG_NAME = "failure_log.md"

# Project profile: produced once at startup, stable across all iterations.
PROJECT_PROFILE_NAME = "project_profile.md"

# Behavioral context synthesized from test content (README substitute for README-less projects).
PROJECT_CONTEXT_NAME = "project_context.md"

# Hash file that invalidates the profile and context cache when project inputs change.
PROJECT_INPUTS_HASH_NAME = "project_profile.inputs_hash"

# Discovery pass output: import paths, type construction patterns, feature flags (BL-001).
COVERAGE_STRATEGY_NAME = "coverage_strategy.md"
COVERAGE_STRATEGY_HASH_NAME = "coverage_strategy.inputs_hash"

# Persistent chunk cache (task1 chunked synthesis); only wiped on explicit --clean.
CHUNK_CACHE_DIR = ".chunk_cache"

# pipeline_state.md phase-key prefixes that belong to iteration state (strip /
# task1+). A "restart from augmented" keeps task0a/0b/0c entries and drops these
# so they re-run from the augmentation commit.
ITERATION_PHASE_PREFIXES: tuple[str, ...] = (
    "strip/", "task1/", "task2-4/", "task2/", "task3/", "task4/",
)


# THE source of truth. `strip_to_tests.py` keeps a byte-equal copy of this set
# rather than importing it, because that module is a standalone stdlib-only CLI
# (`python strip_to_tests.py <dir>`) and coupling it to the package would cost
# that. `tests/test_skip_dirs_parity.py` asserts the two are equal, because the
# copy DRIFTED and the drift was destructive: the canonical set carried
# `.best_tree` and the copy did not, so run `491052-1af6b4f4`'s strip walked 36
# consecutive files inside the recovery slot on 2026-08-08 — `chain.rs`,
# `kind.rs`, `macros.rs` and `nightly.rs` DELETED from the snapshot, the other
# eight stripped back to signatures. Add a name here and the parity test names
# the copy that has not caught up.
SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",            # VCS internals
    "target",                          # Rust / Maven build output
    "node_modules",                    # Node.js dependencies
    "vendor",                          # Go / PHP / Ruby vendored deps
    ".venv", "venv", "env",           # Python virtual environments
    "__pycache__", ".mypy_cache",     # Python caches
    "dist", "build", ".build",        # Generic build output
    ".tox", ".eggs",                   # Python test infra artifacts
    "farf",                            # Solana test ledger artifacts
    SKELETON_DIR, SKELETON_FILLED_DIR, # TASK 2 skeleton backend scratch dirs
    BEST_TREE_DIR,                     # best-tree ratchet snapshot
    ".original_structure",             # pre-strip API snapshot
    ".chunk_cache", ".task1_partials", # task1 caches
    ".phase_c_logs",                   # Phase C test-fix iteration archives
    CLUSTER_AUGMENTED_DIR,             # task0c augmentation sentinels
    MODULE_AUGMENTED_DIR,
    MODULE_STRIPPED_DIR,               # strip sentinel
})


def drop_iteration_phases(phases: dict[str, object]) -> dict[str, object]:
    """Return `phases` without the iteration entries (keys under
    :data:`ITERATION_PHASE_PREFIXES`); task0a/0b/0c entries are kept so those
    tasks skip via their sentinel check on the next run."""
    return {
        k: v for k, v in phases.items()
        if not k.startswith(ITERATION_PHASE_PREFIXES)
    }


# --- the PRE-augmentation restore point ------------------------------------
# `task0c-augmented-<module>` marks the commit AFTER task0c: a restore to it
# brings the generated tests back from the tag, so a run whose test generator
# or admission rule changed had no restore point short of `--repo <URL>
# --clean` — a re-clone that bypasses continue.sh's pre-run gate. This tag
# marks the commit task0c BUILDS ON (the `pipeline: task0b done` commit in a
# git-managed workspace), written once, right before the cluster augmentation
# starts. It sits after task0a/0b rather than at the clone because their
# outputs (project profile, behavioural context, coverage strategy,
# build_configs, the cfg scan) are computed from the ORIGINAL source and tests,
# never from the generated ones — so re-running task0c cannot invalidate them,
# and keeping them costs nothing while regenerating them costs LLM calls.
PRE_AUGMENT_TAG_PREFIX = "task0c-pre-"

# Written by task0c's attempt ledger (`application/tasks/task0c.py` keeps its
# own `TASK0C_ATTEMPTS_LOG` copy — that module owns the writer; this name is
# what the pre-augmentation restore wipes, and a test pins the two equal).
TASK0C_ATTEMPTS_LOG_NAME = ".task0c_attempts.jsonl"

# pipeline_state.md entries a pre-augmentation restore KEEPS: exactly the
# phases that ran before task0c. A keep-list rather than a drop-list, because
# every phase added after task0c (a future task0d, the bake, strip, task1, the
# reconstruct loop) is by construction something the restore must forget, and
# a drop-list would have to be extended for each of them.
PRE_AUGMENT_KEPT_PHASES: tuple[str, ...] = ("cfg_scan", "test_cmd", "task0b")
PRE_AUGMENT_KEPT_PHASE_PREFIXES: tuple[str, ...] = ("task0a",)   # task0a, task0a-context


def pre_augment_tag(module_name: str) -> str:
    """The pre-augmentation tag for `module_name`: `task0c-pre-<module>`."""
    return f"{PRE_AUGMENT_TAG_PREFIX}{module_name}"


def drop_pre_augment_phases(phases: dict[str, object]) -> dict[str, object]:
    """Return only the entries of `phases` that predate task0c
    (:data:`PRE_AUGMENT_KEPT_PHASES` + keys under
    :data:`PRE_AUGMENT_KEPT_PHASE_PREFIXES`). Everything task0c and later wrote
    — `task0c/<cluster>`, `task0c/<cluster>/<module>`, strip, task1, the
    reconstruct loop — is dropped so it re-runs from the pre-augmentation
    commit."""
    return {
        k: v for k, v in phases.items()
        if k in PRE_AUGMENT_KEPT_PHASES or k.startswith(PRE_AUGMENT_KEPT_PHASE_PREFIXES)
    }


def augmentation_has_started(phases: Iterable[str]) -> bool:
    """True once any `task0c` phase entry exists — the tell that HEAD has moved
    past (or into) the augmentation, so it is no longer a pre-augmentation
    commit and must not be tagged as one."""
    return any(k == "task0c" or k.startswith("task0c/") for k in phases)


@dataclass(frozen=True, slots=True)
class PreAugmentTagPlan:
    """Which `task0c-pre-*` tags to write at HEAD before task0c starts.

    `to_write` are the modules' tags not yet in the workspace; `present` the
    ones already there (never re-pointed: `git tag -f` at a later HEAD would
    silently move the restore point onto a post-augmentation commit, and on a
    resumed run HEAD is exactly that); `refusal` is set when HEAD cannot be a
    pre-augmentation commit at all, in which case nothing is written."""

    to_write: tuple[str, ...]
    present: tuple[str, ...]
    refusal: str | None = None

    def summary(self) -> str:
        """The one log line the orchestrator prints for this decision."""
        if self.refusal:
            return f"NOT tagged: {self.refusal}"
        parts: list[str] = []
        if self.to_write:
            parts.append(f"tagged {', '.join(self.to_write)} at HEAD "
                         "(pre-augmentation restore point for --restart-from-pre-augment)")
        if self.present:
            parts.append(f"{', '.join(self.present)} already present — HEAD not re-tagged")
        return "; ".join(parts) or "no modules to tag"


def plan_pre_augment_tags(
    modules: Iterable[str], existing_tags: Iterable[str], phases: Mapping[str, object],
) -> PreAugmentTagPlan:
    """Decide the pre-augmentation tags for `modules` given the workspace's
    existing tags and its pipeline_state phases. Pure.

    A workspace built before the tag existed (the live `tmp/anyhow`, whose
    HEAD is a task2a checkpoint) must NOT be tagged at HEAD: the restore would
    then "return" to a stripped tree and run task0c against no source. Such a
    workspace is tagged by hand at its `pipeline: task0b done` commit, which
    the refusal text says."""
    started = sorted(k for k in phases if k == "task0c" or k.startswith("task0c/"))
    if started:
        shown = ", ".join(started[:3]) + (", …" if len(started) > 3 else "")
        return PreAugmentTagPlan(
            to_write=(), present=(),
            refusal=(f"task0c has already run in this workspace (phase(s) {shown}), so HEAD is "
                     "not a pre-augmentation commit. To add the restore point by hand, tag the "
                     "`pipeline: task0b done` commit: git tag task0c-pre-<module> <sha>"),
        )
    existing = set(existing_tags)
    wanted = [pre_augment_tag(m) for m in modules]
    return PreAugmentTagPlan(
        to_write=tuple(t for t in wanted if t not in existing),
        present=tuple(t for t in wanted if t in existing),
    )

# Legacy whole-workspace manifest of generated files.
MANIFEST_NAME = ".generated_files.txt"

# Workspace directory for cloned/copied project repos (relative to pipeline root).
WORKSPACE_DIR = "tmp"
