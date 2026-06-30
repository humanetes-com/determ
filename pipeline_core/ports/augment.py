"""Role-based ports for the TASK 0c cluster-augmentation phase (R2.2).

The DIP seams the `application.cluster_augment.ClusterAugmentor` use-case
depends on. Each Protocol is a narrow ROLE (ISP) — the use-case receives only
the roles it needs, never a god-deps. `pipeline.py` functions satisfy them
structurally at the composition root; tests pass fakes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline_core.domain.models import BuildCluster


class TestAugmentor(ABC):
    """Language-agnostic interface for per-module coverage augmentation (FR-S5).
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
    ) -> tuple[float, list[Any], int, int]:
        """Measure test coverage. Returns (coverage_pct, files_data, covered_lines, coverable_lines)."""

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
    ) -> bool: ...
    def mark_cluster_augmented(
        self, project_dir: Path, cluster_name: str, module_name: str, cov: float,
    ) -> None: ...
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


@runtime_checkable
class CommitGate(Protocol):
    """The atomic commit-then-mark transition at each cluster boundary."""

    def commit_and_mark(
        self, project_dir: Path, phase_key: str, status: str, *,
        commit_message: str, detail: str = ..., source_hash: str | None = ...,
    ) -> None: ...
