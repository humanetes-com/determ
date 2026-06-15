"""Role-based ports for the TASK-1 requirements synthesizers (debt #14).

`application.generators.task1_synthesizer` use-cases depend on these narrow ROLE
Protocols (ISP) instead of the older `configure()` module-global injection seam.
`pipeline.py` binds the concrete functions at the composition root (the single
`_Task2Adapter`); tests pass fakes. Pure helpers (domain text/output, retry, io, log,
`chunk_cache`) stay direct imports — only the LLM call, the prompt-shaping machinery,
the progress widget, and the requirements-doc I/O (all `pipeline.py`-resident) cross
a port.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Task1LLM(Protocol):
    """The self-hosted model call (Hard Rule 7: local only)."""

    def ask(self, prompt: str, *, model: str | None = ...) -> str: ...


@runtime_checkable
class PromptShaping(Protocol):
    """Prompt-budget arithmetic + test-file rendering/chunking + API-surface
    section formatting (the pieces that size and assemble the prompt body)."""

    def prompt_budget(self, *fixed_parts: str) -> int: ...
    def format_test_files(self, test_files: Any) -> str: ...
    def chunk_test_files_by_file(
        self, test_files: Any, file_char_limit: int,
    ) -> list[Any]: ...
    def format_api_surface_section(self, api_surface: str) -> str: ...


@runtime_checkable
class ProgressHandle(Protocol):
    """Per-chunk timing/progress widget returned by `Progress.make_progress`."""

    def start_chunk(self) -> None: ...
    def finish_chunk(self, current: int) -> None: ...
    def finish(self) -> None: ...


@runtime_checkable
class Progress(Protocol):
    """Factory for the per-run chunk-progress widget."""

    def make_progress(self, total: int) -> ProgressHandle: ...


@runtime_checkable
class Requirements(Protocol):
    """Requirements-doc derived facts + the notes-preserving FR write."""

    def extract_type_names(self, text: str) -> set[str]: ...
    def write_fr_preserving_notes(self, path: Path, new_content: str) -> None: ...
    def type_signals_read(self, project_dir: Path) -> str: ...
