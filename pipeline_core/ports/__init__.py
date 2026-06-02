"""ports — the interfaces the application layer depends on (DIP seams).

Structural ``typing.Protocol``s: adapters satisfy them by shape, no base class needed.
These are the *target* boundaries; `pipeline.py` is migrated onto them slice by slice.
Nothing here does I/O — these only declare the contract that adapters implement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLM(Protocol):
    """A served language model. The single call the pipeline makes against a model."""

    def ask(self, prompt: str, *, system: str | None = ..., think: bool = ...) -> str:
        """Return the model's completion for ``prompt`` (already budget-guarded)."""
        ...


@runtime_checkable
class Extractor(Protocol):
    """The Rust skeleton_extractor, invoked over its JSON CLI contract.

    The seam is sacred (see CODE_ARCHITECTURE.md §4): each method maps to one subcommand
    and returns the parsed JSON record(s). Bodies are never read after strip.
    """

    def skeletonise(self, source: str) -> str: ...
    def enumerate_fns(self, source: str) -> list[dict]: ...
    def enumerate_types(self, source: str) -> list[dict]: ...
    def enumerate_dispatch_families(self, source: str) -> list[dict]: ...
    def enumerate_feature_gate_cfgs(self, source: str) -> list[str]: ...
    def merge_cluster_files(self, files: dict[str, str]) -> str: ...


@runtime_checkable
class Cargo(Protocol):
    """cargo build/test for a workspace under a given cluster environment."""

    def check(self, project_dir: Path, *, env_extra: dict[str, str], features: list[str], toolchain: str | None) -> "ProcessResult": ...
    def test(self, project_dir: Path, *, env_extra: dict[str, str], features: list[str], toolchain: str | None) -> "ProcessResult": ...


@runtime_checkable
class Coverage(Protocol):
    """A coverage backend (tarpaulin / grcov / llvm-cov)."""

    def measure(self, project_dir: Path, *, env_extra: dict[str, str]) -> dict[str, float]:
        """Return per-file (or per-fn) coverage fractions for the workspace."""
        ...


@runtime_checkable
class Workspace(Protocol):
    """The git-managed project workspace under tmp/. Reversible mutation only."""

    root: Path

    def read(self, rel: str) -> str: ...
    def write(self, rel: str, content: str) -> None: ...
    def commit(self, message: str, *, tag: str | None = ...) -> None: ...
    def reset_clean(self) -> None: ...


class ProcessResult(Protocol):
    """Result of a cargo/git subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
