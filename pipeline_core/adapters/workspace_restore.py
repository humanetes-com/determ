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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.workspace_layout import (
    FAILURE_LOG_NAME,
    MODULE_STRIPPED_DIR,
    RECONSTRUCTION_DIRS,
    drop_iteration_phases,
)

# Archives land OUTSIDE project_dir, as a sibling. Two reasons, both learned:
# `git_commit`'s `git add -A` would sweep an in-workspace archive into the next
# task0c/task1 commit, and a later `--clean` re-clone is `shutil.rmtree(dest)`
# over the workspace. The reference floor already survives by the same trick
# (`tmp/.reference_anyhow/` is a sibling of project_dir).
ARCHIVE_DIR_SUFFIX = ".pre_restart"


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

    def _archive_reconstruction(self, project_dir: Path) -> Path | None:
        """Move the reconstruction directories aside before the hard reset, and
        return where they went (None when there was nothing to archive).

        `git reset --hard <task0c-augmented-*>` is a WHOLE-TREE rollback, and
        `.skeleton-filled`, `.skeleton` and `.best_tree` are tracked while the
        tag predates every fill — so the reset deletes them rather than leaving
        them alone. On 2026-08-08 that removed 5,137 files, 5,067 of them
        `.skeleton-filled`, taking a tree that had reached `pass=95 fail=15
        oracle=110` off disk between one run and the next. (The tree itself was
        still in the object store at the task2a checkpoint commit, but nobody
        knew: the checkpoint's message names a score and its artefact sits under
        `.skeleton-filled/<cluster>/src`, not `src/`.)

        Contrast `_wipe_iteration_artifacts` below, which is explicit — it names
        four paths and logs every deletion. The reset was removing a thousand
        times as much and logging one line.

        A rename, not a copy: same filesystem, so it is instant regardless of
        the ~361 MB of build output under `.skeleton-filled`, and it cannot fail
        half-way and leave a partial archive standing in for a real one.
        Deliberately never pruned — an unbounded archive costs disk, and pruning
        is the class of operation this whole item exists because of."""
        present = [d for d in RECONSTRUCTION_DIRS if (project_dir / d).is_dir()]
        if not present:
            return None
        # The destination must be EMPTY, not merely created. `shutil.move` into
        # an existing directory moves the source INSIDE it, so a second restart
        # landing on the same timestamp would produce
        # `<stamp>/.skeleton-filled/.skeleton-filled` and leave the first
        # archive's tree buried under the second's. Two restarts inside one
        # second is exactly what a crash-relaunch loop does.
        root = project_dir.parent / f"{project_dir.name}{ARCHIVE_DIR_SUFFIX}"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest, n = root / stamp, 1
        while dest.exists():
            n += 1
            dest = root / f"{stamp}-{n}"
        dest.mkdir(parents=True)
        for name in present:
            shutil.move(str(project_dir / name), str(dest / name))
        self.log(f"  [restart-from-augmented] ARCHIVED {', '.join(present)} -> {dest}")
        self.log("    the reset below would have deleted these; the reconstruction "
                 "is there, and .skeleton/<cluster> + .skeleton-filled/<cluster> "
                 "must be restored as a PAIR (t1.5 re-authors the layout per run, "
                 "so bodies from one run do not compile against another's skeleton).")
        return dest

    def run(self, root: Path, project_dir: Path) -> None:
        """Restore workspace to the task0c augmentation commit and clear only
        iteration state (strip / task1+ artefacts). Task0a/0b/0c outputs kept."""
        restore_tag = self._find_restore_tag(project_dir)
        module_name = restore_tag.split("-")[-1]

        self._archive_reconstruction(project_dir)

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
