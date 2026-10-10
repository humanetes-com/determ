"""Role-based ports for the TASK 0c cluster-augmentation phase.

The DIP seams the `application.cluster_augment.ClusterAugmentor` use-case
depends on. Each Protocol is a narrow ROLE (ISP) — the use-case receives only
the roles it needs, never a god-deps. `pipeline.py` functions satisfy them
structurally at the composition root; tests pass fakes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.file_coverage_floor import FileFloor
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.models import AugmentVerdict, BuildCluster


class TestAugmentor(ABC):
    """Language-agnostic interface for per-module coverage augmentation.
    The concrete cargo/tarpaulin impl lives in `adapters.augment_engine`."""

    @abstractmethod
    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the module's library target compiles. Returns (ok, error_output)."""

    @abstractmethod
    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the full test harness (lib + tests) compiles. Returns (ok, error_output)."""

    @abstractmethod
    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path,
    ) -> CoverageMeasurement:
        """Measure test coverage: the line fields (pct, files, covered,
        coverable) and, when the backend reports them, the function counts."""

    @abstractmethod
    def is_applicable(self, crate_type: str) -> bool:
        """Return False if this augmentor cannot handle the given crate type."""


@runtime_checkable
class AugmentTelemetry(Protocol):
    """Human log line, structured event, phase marker, and resumable 'current'."""

    def log(self, msg: str) -> None: ...
    def emit(self, event: str, **fields: Any) -> None: ...
    def mark_phase(
        self, project_dir: Path, phase_key: str, status: str, *,
        detail: str = ..., source_hash: str | None = ...,
    ) -> None: ...
    def set_current(self, project_dir: Path, label: str) -> None: ...


@runtime_checkable
class ClusterScope(Protocol):
    """The active-cluster seam: pipeline.py's `_current_cluster` global that the
    downstream task0c prompt/env builders read. The use-case never touches the
    global directly — it announces the active cluster through this port."""

    def set_active(self, cluster: BuildCluster | None) -> None: ...


@runtime_checkable
class AugmentStore(Protocol):
    """Per-(cluster, module) and per-module augmentation sentinels."""

    def is_cluster_augmented(
        self, project_dir: Path, cluster_name: str, module_name: str,
    ) -> bool:
        """Whether a sentinel exists at all — NOT whether it still binds. The
        cluster loop asks `cluster_augmented_verdict(...).still_binding(code_id)`
        before skipping work; this stays the question for "did we write one"."""
        ...
    def cluster_augmented_verdict(
        self, project_dir: Path, cluster_name: str, module_name: str,
    ) -> AugmentVerdict | None:
        """The recorded verdict, or None when no sentinel exists."""
        ...
    def mark_cluster_augmented(
        self, project_dir: Path, cluster_name: str, module_name: str, cov: float,
        *, reason: str = "", code_id: str = "",
        functions: FnCoverageRecord | None = None,
        file_floor: FileFloor | None = None,
    ) -> None:
        """Record the coverage, the terminating status ("done" / "ceiling" /
        "accounted" / "hard_block" / "precheck_failed") and the pipeline `code_identity` it
        was reached under. A give-up reason recorded without a code_id can
        never bind again — that is the intended reading of an unattributable
        verdict. The verdict is stamped with the function-execution criterion
        and `functions`' summary; a `done` without the stamp does not bind."""
        ...
    def cluster_fn_record(
        self, project_dir: Path, cluster_name: str, module_name: str,
    ) -> FnCoverageRecord | None:
        """The function-execution record augmentation persisted for (cluster,
        module), or None."""
        ...
    def cluster_file_floor(
        self, project_dir: Path, cluster_name: str, module_name: str,
    ) -> FileFloor | None:
        """The per-file floor reading augmentation persisted for (cluster,
        module), or None."""
        ...
    def module_file_floors(
        self, project_dir: Path, module_name: str,
    ) -> dict[str, FileFloor]:
        """Every cluster's per-file floor reading for the module, by cluster."""
        ...
    def cluster_augmented_value(
        self, project_dir: Path, cluster_name: str, module_name: str,
    ) -> float:
        """The float coverage recorded in the cluster sentinel (0.0 if missing
        or unparseable)."""
        ...
    def is_module_augmented(self, project_dir: Path, module_name: str) -> bool: ...
    def mark_module_augmented(
        self, project_dir: Path, module_name: str, cov: float,
    ) -> None: ...
    def clear_module_sentinel(self, project_dir: Path, module_name: str) -> None: ...


@runtime_checkable
class AugmentEngine(Protocol):
    """The cluster's coverage-augmentation work: ceiling estimate, base-compile
    pre-check, the current test file set, and the per-module augment driver."""

    def estimate_ceiling(
        self, project_dir: Path, cluster: BuildCluster,
    ) -> tuple[float, int, int]: ...
    def pre_check(
        self, project_dir: Path, module_name: str, cluster: BuildCluster,
    ) -> bool: ...
    def collect_test_rels(self, project_dir: Path) -> set[str]: ...
    def augment_module(
        self, project_dir: Path, module_name: str, module_dir: Path, crate_type: str,
        *, existing_test_files: set[str], project_context: str, coverage_strategy: str,
    ) -> tuple[float, int, bool, str]: ...
    def behaviour_coverage(
        self, project_dir: Path, cluster: BuildCluster, module_name: str, module_dir: Path,
    ) -> tuple[int, int]:
        """`(pinned, total)` over the cluster's harvested behaviour suite: how
        many of its pinnable behaviours some `*.gen_tests_*.rs` of the module
        asserts exactly. `(0, 0)` when the suite lists nothing pinnable."""
        ...
    def behaviour_pass(
        self, project_dir: Path, cluster: BuildCluster, module_name: str, module_dir: Path,
        *, project_context: str, coverage_strategy: str,
    ) -> bool:
        """One bounded generation pass over the module's files that still have
        unpinned behaviours, each block held to the pin contract; True when at
        least one block was admitted. Costs one model call per such file."""
        ...


@runtime_checkable
class CommitGate(Protocol):
    """The atomic commit-then-mark transition at each cluster boundary."""

    def commit_and_mark(
        self, project_dir: Path, phase_key: str, status: str, *,
        commit_message: str, detail: str = ..., source_hash: str | None = ...,
    ) -> None: ...
