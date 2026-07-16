"""FileAugmentStore — the on-disk TASK 0c augmentation sentinels (P6).

Satisfies :class:`pipeline_core.ports.augment.AugmentStore`. Each (cluster,
module) and each module records the bare numeric coverage it reached in a
sentinel file under the workspace, so a resumed run skips already-augmented work.
Pure file I/O — the only state is the workspace path passed per call. Relocated
verbatim from the pipeline.py free functions + the `_AugmentAdapter` binding so
the use-case is injected with the real concrete (no composition-root shim)."""

from __future__ import annotations

from pathlib import Path

from ..domain.workspace_layout import CLUSTER_AUGMENTED_DIR, MODULE_AUGMENTED_DIR


def _read_cov(path: Path) -> float:
    """The numeric coverage in a sentinel file, or 0.0 if missing/unparseable."""
    try:
        return float(path.read_text().strip())
    except (ValueError, OSError):
        return 0.0


class FileAugmentStore:
    """Sentinel-file persistence for cluster/module augmentation state."""

    def cluster_path(self, project_dir: Path, cluster_name: str, module_name: str) -> Path:
        return project_dir / CLUSTER_AUGMENTED_DIR / f"{cluster_name}__{module_name}"

    def module_path(self, project_dir: Path, module_name: str) -> Path:
        return project_dir / MODULE_AUGMENTED_DIR / module_name

    # --- AugmentStore port ---
    def is_cluster_augmented(self, project_dir: Path, cluster_name: str, module_name: str) -> bool:
        return self.cluster_path(project_dir, cluster_name, module_name).exists()

    def mark_cluster_augmented(self, project_dir: Path, cluster_name: str,
                               module_name: str, cov: float) -> None:
        p = self.cluster_path(project_dir, cluster_name, module_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{cov:.1f}\n")   # bare numeric (no '%' text round-trip)

    def cluster_augmented_value(self, project_dir: Path, cluster_name: str,
                                module_name: str) -> float:
        return _read_cov(self.cluster_path(project_dir, cluster_name, module_name))

    def is_module_augmented(self, project_dir: Path, module_name: str) -> bool:
        return self.module_path(project_dir, module_name).exists()

    def mark_module_augmented(self, project_dir: Path, module_name: str, cov: float) -> None:
        p = self.module_path(project_dir, module_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{cov:.1f}\n")   # bare numeric (no '%' text round-trip)

    def module_augmented_value(self, project_dir: Path, module_name: str) -> float:
        return _read_cov(self.module_path(project_dir, module_name))

    def clear_module_sentinel(self, project_dir: Path, module_name: str) -> None:
        self.module_path(project_dir, module_name).unlink(missing_ok=True)
