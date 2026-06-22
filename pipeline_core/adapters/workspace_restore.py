"""Restore a workspace to its task0c augmentation commit (P6 engine-relocation series).

`--restart-from-augmented` resets the workspace git tree to the single
`task0c-augmented-*` tag, then wipes only iteration artefacts (strip / task1+)
while keeping the task0a/0b/0c outputs. Single-module workspaces only — errors
loudly if the tag is missing or more than one module was augmented.

Lifted verbatim out of the pipeline.py god-file; the composition root injects the
state read/write port and the log sink. The pure vocabulary (iteration-phase
prefixes, sentinel/file names, the phase-drop rule) lives in
:mod:`pipeline_core.domain.workspace_layout`.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.workspace_layout import (
    FAILURE_LOG_NAME,
    MODULE_STRIPPED_DIR,
    drop_iteration_phases,
)


@dataclass(frozen=True, slots=True)
class RestoreFromAugmented:
    """Reset-to-augmentation-tag operation with its collaborators injected."""

    read_state: Callable[[Path], dict[str, Any]]
    write_state: Callable[[Path, dict[str, Any]], None]
    log: Callable[[str], None]

    def _find_restore_tag(self, project_dir: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "tag", "-l", "task0c-augmented-*"],
                cwd=project_dir, capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git tag listing failed: {e}") from e
        tags = [t for t in result.stdout.strip().splitlines() if t]
        if not tags:
            raise RuntimeError(
                "No task0c-augmented-* tag in the workspace. "
                "There is no augmented restore point — use --clean for a full wipe instead."
            )
        if len(tags) > 1:
            raise RuntimeError(
                f"Multi-module restore not supported in this version. Found tags: {tags}. "
                "Use --clean for a full wipe."
            )
        return tags[0]

    def _wipe_iteration_artifacts(self, root: Path, project_dir: Path, module_name: str) -> None:
        iteration_paths: list[Path] = [
            project_dir / MODULE_STRIPPED_DIR,         # strip sentinel
            project_dir / f"functional_requirements_{module_name}.md",
            project_dir / FAILURE_LOG_NAME,
            project_dir / ".task1_partials",
        ]
        iteration_paths += list(project_dir.glob(".generated_files_*.txt"))
        iteration_paths += list(project_dir.glob("tests_output_*.txt"))
        for p in iteration_paths:
            shown = p.relative_to(root) if p.is_relative_to(root) else p
            if p.is_dir():
                shutil.rmtree(p)
                self.log(f"  Deleted {shown}/")
            elif p.exists():
                p.unlink()
                self.log(f"  Deleted {shown}")

    def run(self, root: Path, project_dir: Path) -> None:
        """Restore workspace to the task0c augmentation commit and clear only
        iteration state (strip / task1+ artefacts). Task0a/0b/0c outputs kept."""
        restore_tag = self._find_restore_tag(project_dir)
        module_name = restore_tag.split("-")[-1]

        self.log(f"  [restart-from-augmented] resetting workspace to {restore_tag}")
        subprocess.run(["git", "reset", "--hard", restore_tag],
                       cwd=project_dir, check=True, capture_output=True)

        self._wipe_iteration_artifacts(root, project_dir, module_name)

        # Pipeline state file: clear strip/task1+ entries so they re-run; keep
        # task0a/0b/0c entries so those tasks skip via sentinel check.
        state = self.read_state(project_dir)
        state["phases"] = drop_iteration_phases(state["phases"])
        state["current"] = "restart-from-augmented"
        self.write_state(project_dir, state)
        self.log("  Cleared iteration entries from pipeline_state.md")
        self.log(f"  Workspace restored to {restore_tag}; "
                 "task0a/0b/0c kept, strip + task1+ will re-run.")
