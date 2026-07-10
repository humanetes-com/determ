"""Workspace layout vocabulary — the well-known directory names the pipeline
writes under a project workspace. Pure constants (stdlib only); the single
source of truth so adapters and the composition root agree on the on-disk layout
instead of each repeating a string literal.

(``ORIGINAL_STRUCTURE_DIR`` currently lives in ``adapters/strip.py`` for
historical reasons; consolidate it here in a later slice.)"""
from __future__ import annotations

# Per-module locked source + the `locked.txt` ledger live here once a module's
# tests pass and it is frozen as a dependency for later modules.
LOCKED_MODULES_DIR = ".locked_modules"

# TASK 0c augmentation sentinels: one file per (cluster, module) and one per
# module, each holding the bare numeric coverage reached (R8.4 — no '%' text).
CLUSTER_AUGMENTED_DIR = ".cluster_augmented"
MODULE_AUGMENTED_DIR = ".module_augmented"

# Strip sentinel dir: present once a module's source has been skeletonised.
MODULE_STRIPPED_DIR = ".module_stripped"

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


SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",            # VCS internals
    "target",                          # Rust / Maven build output
    "node_modules",                    # Node.js dependencies
    "vendor",                          # Go / PHP / Ruby vendored deps
    ".venv", "venv", "env",           # Python virtual environments
    "__pycache__", ".mypy_cache",     # Python caches
    "dist", "build", ".build",        # Generic build output
    ".tox", ".eggs",                   # Python test infra artifacts
    ".skeleton", ".skeleton-filled",  # TASK 2 skeleton backend scratch dirs
    ".best_tree",                      # best-tree ratchet snapshot
    ".original_structure",             # pre-strip API snapshot
    ".chunk_cache", ".task1_partials", # task1 caches
    ".phase_c_logs",                   # Phase C test-fix iteration archives (P4)
})


def drop_iteration_phases(phases: dict[str, object]) -> dict[str, object]:
    """Return `phases` without the iteration entries (keys under
    :data:`ITERATION_PHASE_PREFIXES`); task0a/0b/0c entries are kept so those
    tasks skip via their sentinel check on the next run."""
    return {
        k: v for k, v in phases.items()
        if not k.startswith(ITERATION_PHASE_PREFIXES)
    }

# Legacy whole-workspace manifest of generated files.
MANIFEST_NAME = ".generated_files.txt"
