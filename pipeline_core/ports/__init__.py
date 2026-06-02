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

    The real CLI is *path-based*, not source-string-based: every subcommand takes an
    ``--input <dir>`` over a live source tree, plus optional per-item cfg filtering
    (``--cfg-true PRED`` / ``--cfg-false PRED``) derived from the active cluster. The
    ``enumerate_*`` methods return ``None`` (not raise) when the binary is unavailable or
    the subprocess fails — callers treat that as "no records".
    """

    def enumerate_fns(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[dict] | None: ...
    def enumerate_types(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[dict] | None: ...
    def enumerate_dispatch_families(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[dict] | None: ...
    def enumerate_feature_gate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[str] | None: ...
    def skeletonise(self, input_dir: Path, output_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> dict: ...
    def merge_clusters(self, out_path: Path, cluster_files: list[tuple[str, Path, str]]) -> bool: ...


@runtime_checkable
class Cargo(Protocol):
    """cargo invocation primitive: run one cargo command in a workspace.

    Matches the real seam (``pipeline._run_cargo``): one shell command, a working dir, an
    explicit environment, a timeout. Higher-level concerns (deriving the command from a
    cluster's features/toolchain, JSON-diagnostic checks) compose on top in the
    application layer — they are not part of this port.
    """

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = ...) -> "ProcessResult": ...


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


@runtime_checkable
class ProcessResult(Protocol):
    """Result of a cargo/git subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
