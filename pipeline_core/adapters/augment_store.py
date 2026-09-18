"""FileAugmentStore — the on-disk TASK 0c augmentation sentinels.

Satisfies :class:`pipeline_core.ports.augment.AugmentStore`. Each (cluster,
module) and each module records in a sentinel file under the workspace the
coverage it reached, so a resumed run skips already-augmented work. A CLUSTER
sentinel also records WHY augmentation stopped and the pipeline `code_identity`
it stopped under (`AugmentVerdict`), because a give-up verdict is a claim about
the pipeline rather than about the tree and must not outlive it; a module
sentinel stays a bare number, since it answers "has task0c ever run here" — a
fact about history that the task0d harvest gate depends on staying sticky.
Pure file I/O — the only state is the workspace path passed per call. Relocated
verbatim from the pipeline.py free functions + the `_AugmentAdapter` binding so
the use-case is injected with the real concrete (no composition-root shim)."""

from __future__ import annotations

import os
from pathlib import Path

from ..domain.fn_execution import FnCoverageRecord
from ..domain.models import FN_EXECUTION_CRITERION, AugmentVerdict
from ..domain.workspace_layout import CLUSTER_AUGMENTED_DIR, MODULE_AUGMENTED_DIR


def _read_cov(path: Path) -> float:
    """The numeric coverage in a sentinel file, or 0.0 if missing/unparseable.

    Reads the FIRST line only: a cluster sentinel now carries `reason=` and
    `code_id=` lines after the number (see `AugmentVerdict`), and a module
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
                               reason: str = "", code_id: str = "",
                               functions: FnCoverageRecord | None = None) -> None:
        """Write the verdict, stamped with the function-execution criterion
        the current stop applies and `functions`' summary (none when nothing
        could be measured). A verdict on disk WITHOUT the stamp was written by
        a line-coverage stop, and a `done` of that kind does not bind again
        (`AugmentVerdict.still_binding`)."""
        p = self.cluster_path(project_dir, cluster_name, module_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = functions if functions is not None and functions.measured else None
        p.write_text(AugmentVerdict(
            cov, reason, code_id,
            criterion=FN_EXECUTION_CRITERION,
            fn_measured=rec is not None,
            unexecuted=len(rec.pending) if rec is not None else 0,
            unreached=tuple(f"{u.site.file}::{u.site.fn_name}"
                            + (f" [{u.site.container}]" if u.site.container else "")
                            for u in (rec.unreached if rec is not None else ())),
        ).to_text())

    def fn_record_path(self, project_dir: Path, cluster_name: str,
                       module_name: str) -> Path:
        return (project_dir / CLUSTER_AUGMENTED_DIR
                / f"{cluster_name}__{module_name}.fns.json")

    def cluster_fn_record(self, project_dir: Path, cluster_name: str,
                          module_name: str) -> FnCoverageRecord | None:
        """The function-execution record augmentation wrote for (cluster,
        module); None when absent or written under another criterion."""
        p = self.fn_record_path(project_dir, cluster_name, module_name)
        try:
            return FnCoverageRecord.from_json(p.read_text(encoding="utf-8"))
        except OSError:
            return None

    def save_cluster_fn_record(self, project_dir: Path, cluster_name: str,
                               module_name: str, record: FnCoverageRecord) -> None:
        """Atomic: the record is read back on resume, and a torn write would
        hand every function a fresh attempt budget."""
        p = self.fn_record_path(project_dir, cluster_name, module_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(record.to_json(), encoding="utf-8")
        os.replace(tmp, p)

    def all_fn_records(self, project_dir: Path) -> list[FnCoverageRecord]:
        """Every cluster's function record in the workspace, for the gates
        that classify a stub against what augmentation measured."""
        d = project_dir / CLUSTER_AUGMENTED_DIR
        out: list[FnCoverageRecord] = []
        for p in sorted(d.glob("*.fns.json")) if d.is_dir() else ():
            try:
                rec = FnCoverageRecord.from_json(p.read_text(encoding="utf-8"))
            except OSError:
                continue
            if rec is not None:
                out.append(rec)
        return out

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
