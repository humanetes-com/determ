"""Role-based ports for the per-module reconstruction use-cases.

These are the DIP seams the `application.reconstruct.module_loop` use-cases
depend on. Each Protocol is a narrow ROLE (ISP) — a use-case receives only the
roles it needs, never a god-deps. `pipeline.py` functions satisfy them
structurally at the composition root; tests pass fakes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline_core.domain.generation import Task1Request


@runtime_checkable
class Telemetry(Protocol):
    """Human-facing log line, structured event, and the resumable 'current step'."""

    def log(self, msg: str) -> None: ...
    def emit(self, event: str, **fields: Any) -> None: ...
    def set_current(self, project_dir: Path, label: str) -> None: ...


@runtime_checkable
class PhaseGate(Protocol):
    """pipeline_state phase markers + the atomic commit-then-mark transition."""

    def phase_valid(self, project_dir: Path, phase_key: str) -> bool: ...
    def commit_and_mark(
        self, project_dir: Path, phase_key: str, status: str, *,
        commit_message: str, tag: str | None = ..., detail: str = ...,
        source_hash: str | None = ...,
    ) -> None: ...


@runtime_checkable
class RequirementsService(Protocol):
    """TASK 1: synthesise functional_requirements + anti-corruption validation."""

    def task1(self, req: Task1Request) -> bool: ...
    def validate_task1_output(
        self, req_path: Path, test_files: Any,
    ) -> tuple[bool, str]: ...


@runtime_checkable
class Generator(Protocol):
    """TASK 2: generate source + anti-corruption + API-surface conformance."""

    def task2_inner_loop(
        self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
        manifest_path: Path, existing_test_files: Any, project_profile: str,
        workspace_structure: str, build_check_cmd: str | None, *,
        locked_dep_context: str = ..., current_module: str = ...,
        module_dir_rel: str = ..., whole_project_context: str = ...,
        coverage_strategy: str = ..., api_surface: str = ...,
        prev_source_context: str = ...,
    ) -> bool: ...
    def load_original_structure(self, project_dir: Path, module_name: str) -> list[str]: ...
    def validate_task2_output(
        self, project_dir: Path, expected_paths: list[str], *, req_path: Path,
    ) -> tuple[bool, str]: ...
    def validate_api_conformance(
        self, project_dir: Path, module_name: str, module_dir: Path,
    ) -> tuple[bool, str]: ...


@runtime_checkable
class TestRunner(Protocol):
    """TASK 3: the per-cluster strict-verdict test runner."""

    def task3(
        self, project_dir: Path, test_command: str, iteration: int | str,
        output_dir: Path | str, genesis_path: Path,
    ) -> tuple[bool, str]: ...


@runtime_checkable
class Fixer(Protocol):
    """The failure classifier. Task 3's failures are answered by the
    per-cluster loop through the module loop's re-entry, not by a fix loop on
    the merged tree."""

    def classify_failure(self, test_output: str) -> str: ...
    def diagnose_no_tests(
        self, project_dir: Path, test_command: str, reason: str, test_output: str,
    ) -> tuple[str, str]: ...


@runtime_checkable
class Analyst(Protocol):
    """TASK 4: failure analysis → genesis update, with stall detection + audit."""

    def task4(
        self, genesis_path: Path, functional_req_path: Path, test_output: str,
        iteration: int | str, output_dir: Path | str, *, failure_type: str = ...,
        failure_log: str = ..., prev_pass_count: int = ..., prev_test_output: str = ...,
        source_context: str = ..., api_surface: str = ...,
    ) -> tuple[bool, int]: ...
    def validate_task4_output(
        self, genesis_before: str, genesis_after: str, test_output: str,
        failure_type: str,
    ) -> tuple[bool, str]: ...
    def genesis_audit(self, genesis_path: Path) -> bool: ...


@runtime_checkable
class FailureJournal(Protocol):
    """The append-only failure log fed back into TASK 4 prompts."""

    def load_failure_log(self, log_path: Path) -> str: ...
    def append_failure_log(
        self, log_path: Path, iteration: int | str, failure_type: str,
        current_pass: int, prev_pass_count: int, summary: str,
    ) -> None: ...


@runtime_checkable
class Ratchet(Protocol):
    """Best-tree ratchet: snapshot on a new-best pass count, restore on regress."""

    def best_tree_pass_count(self, project_dir: Path) -> int | None: ...
    def save_best_tree(
        self, project_dir: Path, files: list[str], pass_count: int, *, iteration: int,
    ) -> int: ...
    def restore_best_tree(self, project_dir: Path) -> list[str]: ...


@runtime_checkable
class Artifacts(Protocol):
    """Read-side workspace artefacts the reconstruction phases consult."""

    def read(self, path: Path) -> str: ...
    def load_api_surface(self, project_dir: Path, module_name: str) -> str: ...
    def load_manifest(self, manifest_path: Path) -> list[str]: ...
    def load_locked_source_context(self, project_dir: Path, dep_names: set[str]) -> str: ...
    def format_whole_project_context(self, test_files: Any, reason: str) -> str: ...
    def collect_source_context(
        self, project_dir: Path, manifest_path: Path, exclude: set[str],
    ) -> str:
        """Concatenate the manifest's generated source files (minus `exclude`) as
        ``<FILE path=…>`` blocks — TASK 4's view of what was generated."""
        ...


@runtime_checkable
class CoverageDocs(Protocol):
    """Regenerate coverage_strategy.md after a genesis update (TASK 0b, forced)."""

    def regenerate(
        self, project_dir: Path, test_files: Any, readme_text: str,
        project_profile: str, clusters: Any,
    ) -> str: ...


@runtime_checkable
class FlowAudit(Protocol):
    """The read-only flow self-auditor, run at each iteration boundary."""

    def run(self, context: str) -> None: ...


@runtime_checkable
class DocsMetrics(Protocol):
    """Per-iteration doc-richness signals for the docs.richness telemetry event."""

    def model_notes(self, fr_path: Path) -> list[str]: ...
    def genesis_rules_count(self, project_dir: Path, module_scope: str) -> int: ...
