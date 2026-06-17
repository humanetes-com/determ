"""BestTreeRatchet — the concrete `ports.reconstruct.Ratchet`.

Snapshots the workspace on a new-best pass count and restores it on a regress.
The ratchet logic already lives in :mod:`pipeline_core.application.workspace_lifecycle`;
this adapter is the boundary object the reconstruction use-cases receive (DI),
replacing the `_ReconstructAdapter` pass-through methods."""
from __future__ import annotations

from pathlib import Path

from ..application.workspace_lifecycle import (
    best_tree_pass_count,
    restore_best_tree,
    save_best_tree,
)


class BestTreeRatchet:
    def best_tree_pass_count(self, project_dir: Path) -> int | None:
        return best_tree_pass_count(project_dir)

    def save_best_tree(self, project_dir: Path, files: list[str], pass_count: int,
                       *, iteration: int) -> int:
        return save_best_tree(project_dir, files, pass_count, iteration=iteration)

    def restore_best_tree(self, project_dir: Path) -> list[str]:
        return restore_best_tree(project_dir)
