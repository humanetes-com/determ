"""Unit tests for the workspace-layout domain rule used by --restart-from-augmented.

`drop_iteration_phases` is the pure rule that decides which pipeline_state phase
entries a restore-to-augmented wipes (strip / task1+) vs keeps (task0a/0b/0c).
"""
from __future__ import annotations

from pipeline_core.domain.workspace_layout import (
    ITERATION_PHASE_PREFIXES,
    drop_iteration_phases,
)


def test_drop_iteration_phases_keeps_task0_drops_iteration():
    phases = {
        "task0a/anyhow": "done",
        "task0b/anyhow": "done",
        "task0c/anyhow": "done",
        "strip/anyhow": "done",
        "task1/anyhow": "done",
        "task2-4/anyhow": "done",
        "task2/anyhow": "done",
        "task3/anyhow": "done",
        "task4/anyhow": "done",
    }
    kept = drop_iteration_phases(phases)
    assert kept == {
        "task0a/anyhow": "done",
        "task0b/anyhow": "done",
        "task0c/anyhow": "done",
    }


def test_drop_iteration_phases_empty():
    assert drop_iteration_phases({}) == {}


def test_iteration_prefixes_are_the_iteration_keys():
    # every declared prefix is dropped; an unrelated key survives.
    phases = {f"{p}m": "x" for p in ITERATION_PHASE_PREFIXES}
    phases["task0c/m"] = "x"
    assert drop_iteration_phases(phases) == {"task0c/m": "x"}
