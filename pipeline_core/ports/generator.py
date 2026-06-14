"""Role-based ports for the TASK-2 source generators (P4).

The `application.generators.task2_generator` use-cases depend on these narrow
ROLE Protocols (ISP) instead of reaching into `pipeline.py` globals. `pipeline.py`
binds the concrete functions at the composition root (`_Task2Adapter`); tests
pass fakes.

Pure helpers the generators also use (prompt-section text, file extraction,
truncation, API-surface/topo parsing) are NOT ports — they live in inner layers
(`domain`, `prompts`) the use-cases import directly. Only I/O, the LLM call, the
subprocess/extractor, and `pipeline.py`-resident orchestration cross a port.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Telemetry(Protocol):
    """Human-facing log line, structured event, graceful-stop check, and the
    resumable phase markers."""

    def log(self, msg: str) -> None: ...
    def emit(self, event: str, *, duration_ms: int | None = ...,
             data: dict[str, Any] | None = ...) -> None: ...
    def check_stop(self, context: str) -> None: ...
    def set_current(self, project_dir: Path, label: str) -> None: ...
    def mark_phase(self, project_dir: Path, key: str, status: str) -> None: ...
    def phase_valid(self, project_dir: Path, key: str) -> bool: ...


@runtime_checkable
class LLM(Protocol):
    """The self-hosted model call (Hard Rule 7: local only)."""

    def ask(self, prompt: str, *, model: str | None = ...) -> str: ...


@runtime_checkable
class Workspace(Protocol):
    """Read/write the workspace tree + manifest + saved structure, and the
    cheap Cargo.toml-shape queries the generators consult."""

    def read(self, path: Path) -> str: ...
    def write_source_files(
        self, files_dict: dict[str, str], project_dir: Path, manifest_path: Path,
        existing_test_files: Any,
    ) -> int: ...
    def load_manifest(self, manifest_path: Path) -> list[str]: ...
    def save_manifest(self, manifest_path: Path, files: list[str]) -> None: ...
    def load_original_structure(
        self, project_dir: Path, module_name: str,
    ) -> list[str]: ...
    def crate_entry_point(
        self, cargo_toml_path: Path, module_dir_rel: str, file_path_set: set[str],
    ) -> str | None: ...
    def is_proc_macro_crate(self, cargo_toml_text: str) -> bool: ...


@runtime_checkable
class ContextDocs(Protocol):
    """The per-module prompt-context documents the generators inject: downstream
    API, known compile pitfalls, structured genesis rules, test assertions, and
    the pre-strip API surface."""

    def scan_downstream_api(self, project_dir: Path, module_name: str) -> str: ...
    def compile_patterns_read(self, project_dir: Path, module: str) -> str: ...
    def genesis_rules_read(
        self, project_dir: Path, module_scope: str = ...,
    ) -> str: ...
    def extract_test_assertions(
        self, test_files: list[tuple[str, str]], module_dir_rel: str = ...,
    ) -> str: ...
    def format_api_surface_section(self, api_surface: str) -> str: ...
    def split_api_surface_by_file(
        self, api_surface: str,
    ) -> list[tuple[str, str]]: ...
