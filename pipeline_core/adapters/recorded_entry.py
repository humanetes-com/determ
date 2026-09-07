"""File-backed reader of the recorded executed set per test — the
retrospectives store (`.phase_c_logs/retrospectives.json`) loaded through
its one loader, then read by the pure domain reader
(`pipeline_core.domain.recorded_entry`).

Bound into `run_phase_c_tdd` so the coverage block can substitute a
recorded set for a red test whose process did not complete this sweep. The
driver never opens the store itself; a test that stubs this port hands the
driver a mapping.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pipeline_core.adapters.retros import load_retrospectives
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.recorded_entry import recorded_entry_sets

__all__ = ["recorded_entry_sets_for"]


def recorded_entry_sets_for(
    project_dir: Path, tests: Iterable[str],
) -> dict[str, frozenset[FnLocator]]:
    """`recorded_entry_sets` over the project's store on disk. An absent or
    malformed store reads as empty (the loader's contract), so every test
    is then absent and the caller keeps its ignorance."""
    return recorded_entry_sets(load_retrospectives(project_dir), tests)
