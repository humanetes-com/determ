"""CallableTestRunner — the TASK 3 strict-verdict test runner boundary (P6).

Satisfies :class:`pipeline_core.ports.reconstruct.TestRunner`. TASK 3's logic
already lives in ``application.tasks.task3``; the composition root keeps a thin
``task3`` callable that binds the deps web + emits the stage events. This adapter
wraps that callable so the `ReconstructionStep` use-case receives a real,
narrow TestRunner (one of the role slices peeled off the `_ReconstructAdapter`
god-object) instead of the multi-role binding object."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class CallableTestRunner:
    def __init__(self, task3: Callable[..., tuple[bool, str]]) -> None:
        self._task3 = task3

    def task3(self, project_dir: Path, test_command: str, iteration: int | str,
              output_dir: Path | str, genesis_path: Path) -> tuple[bool, str]:
        return self._task3(project_dir, test_command, iteration, output_dir, genesis_path)
