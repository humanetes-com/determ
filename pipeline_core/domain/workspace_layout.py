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

# Failure log: running record of every iteration's outcome (fed into TASK 4).
FAILURE_LOG_NAME = "failure_log.md"

# pipeline_state.md phase-key prefixes that belong to iteration state (strip /
# task1+). A "restart from augmented" keeps task0a/0b/0c entries and drops these
# so they re-run from the augmentation commit.
ITERATION_PHASE_PREFIXES: tuple[str, ...] = (
    "strip/", "task1/", "task2-4/", "task2/", "task3/", "task4/",
)


def drop_iteration_phases(phases: dict[str, object]) -> dict[str, object]:
    """Return `phases` without the iteration entries (keys under
    :data:`ITERATION_PHASE_PREFIXES`); task0a/0b/0c entries are kept so those
    tasks skip via their sentinel check on the next run."""
    return {
        k: v for k, v in phases.items()
        if not k.startswith(ITERATION_PHASE_PREFIXES)
    }
