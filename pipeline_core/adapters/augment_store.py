"""FileAugmentStore — the on-disk TASK 0c augmentation sentinels (P6).

Satisfies :class:`pipeline_core.ports.augment.AugmentStore`. Each (cluster,
module) and each module records in a sentinel file under the workspace the
coverage it reached, so a resumed run skips already-augmented work. A CLUSTER
sentinel also records WHY augmentation stopped and the pipeline source hash it
stopped under (`AugmentVerdict`), because a give-up verdict is a claim about the
code rather than about the tree and must not outlive it; a module sentinel stays
a bare number, since it answers "has task0c ever run here" — a fact about
history that the task0d harvest gate depends on staying sticky.
Pure file I/O — the only state is the workspace path passed per call. Relocated
verbatim from the pipeline.py free functions + the `_AugmentAdapter` binding so
the use-case is injected with the real concrete (no composition-root shim)."""

from __future__ import annotations

from pathlib import Path

from ..domain.models import AugmentVerdict
from ..domain.workspace_layout import CLUSTER_AUGMENTED_DIR, MODULE_AUGMENTED_DIR


def _read_cov(path: Path) -> float:
    """The numeric coverage in a sentinel file, or 0.0 if missing/unparseable.

    Reads the FIRST line only: a cluster sentinel now carries `reason=` and
    `source_hash=` lines after the number (see `AugmentVerdict`), and a module
    sentinel is still a bare number."""
    try:
        return float(path.read_text().strip().splitlines()[0].strip())
    except (ValueError, OSError, IndexError):
        return 0.0


class FileAugmentStore:
    """Sentinel-file persistence for cluster/module augmentation state."""

    def cluster_path(self, project_dir: Path, cluster_name: str, module_name: str) -> Path:
        return project_dir / CLUSTER_AUGMENTED_DIR / f"{cluster_name}__{module_name}"

    def module_path(self, project_dir: Path, module_name: str) -> Path:
        return project_dir / MODULE_AUGMENTED_DIR / module_name

    # --- AugmentStore port ---
    def is_cluster_augmented(self, project_dir: Path, cluster_name: str, module_name: str) -> bool:
        """Whether a sentinel was ever written. Says nothing about whether it is
        still binding — that is `cluster_augmented_verdict(...).still_binding()`,
        which is what the cluster loop asks before skipping the work."""
        return self.cluster_path(project_dir, cluster_name, module_name).exists()

    def cluster_augmented_verdict(self, project_dir: Path, cluster_name: str,
                                  module_name: str) -> AugmentVerdict | None:
        p = self.cluster_path(project_dir, cluster_name, module_name)
        if not p.exists():
            return None
        try:
            return AugmentVerdict.from_text(p.read_text())
        except OSError:
            return None

    def mark_cluster_augmented(self, project_dir: Path, cluster_name: str,
                               module_name: str, cov: float, *,
                               reason: str = "", source_hash: str = "") -> None:
        p = self.cluster_path(project_dir, cluster_name, module_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(AugmentVerdict(cov, reason, source_hash).to_text())

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
