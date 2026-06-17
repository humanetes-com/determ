"""ports — the interfaces the application layer depends on (DIP seams).

Structural ``typing.Protocol``s: adapters satisfy them by shape, no base class needed.
These are the *target* boundaries; `pipeline.py` is migrated onto them slice by slice.
Nothing here does I/O — these only declare the contract that adapters implement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain.models import (
    CfgOccurrence,
    ConstRecord,
    DispatchFamily,
    FnRecord,
    ModRecord,
    TypeRecord,
)
from ..domain.refine import Attempt, LoopState


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

    def enumerate_fns(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[FnRecord] | None: ...
    def enumerate_types(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[TypeRecord] | None: ...
    def enumerate_dispatch_families(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[DispatchFamily] | None: ...
    def enumerate_feature_gate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[str] | None: ...
    def enumerate_mods(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[ModRecord] | None: ...
    def enumerate_consts(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[ConstRecord] | None: ...
    def enumerate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> list[CfgOccurrence] | None: ...
    def skeletonise(self, input_dir: Path, output_dir: Path, *, cfg_true: tuple[str, ...] = ..., cfg_false: tuple[str, ...] = ...) -> dict[str, Any]: ...
    def merge_clusters(self, out_path: Path, cluster_files: list[tuple[str, Path, str]]) -> bool: ...


@runtime_checkable
class Cargo(Protocol):
    """cargo invocation primitive: run one cargo command in a workspace.

    Matches the real seam (``pipeline._run_cargo``): one shell command, a working dir, an
    explicit environment, a timeout. Higher-level concerns (deriving the command from a
    cluster's features/toolchain, JSON-diagnostic checks) compose on top in the
    application layer — they are not part of this port.
    """

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = ...) -> ProcessResult: ...


@runtime_checkable
class Coverage(Protocol):
    """A coverage backend (tarpaulin / grcov / llvm-cov).

    Matches the relocated provider seam: ``measure`` returns the tarpaulin-compatible
    ``(pct, files_data, covered, coverable)`` tuple that downstream pragma/uncovered-line
    passes consume; ``measure_file`` is the fast per-file snapshot; ``load_cached`` restores
    a prior measurement without re-running.
    """

    def measure(self, project_dir: Path, module_name: str) -> tuple[float, list[dict[str, Any]], int, int]: ...
    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None: ...
    def load_cached(self, project_dir: Path, module_name: str) -> tuple[float, list[dict[str, Any]], int, int] | None: ...


@runtime_checkable
class Workspace(Protocol):
    """The git-managed project workspace under tmp/. Reversible mutation only."""

    root: Path

    def read(self, rel: str) -> str: ...
    def write(self, rel: str, content: str) -> None: ...
    def commit(self, message: str, *, tag: str | None = ...) -> None: ...
    def reset_clean(self) -> None: ...
    def default_branch(self) -> str: ...
    def file_hash(self, filename: str) -> str: ...


@runtime_checkable
class PipelineState(Protocol):
    """The pipeline_state.md store — phase markers + resume position.

    Single source of truth for which phases are complete; the wipe/rerun
    invariant (slice 9) hangs off `phase_valid`.
    """

    def read_state(self, project_dir: Path) -> dict[str, Any]: ...
    def mark_phase(self, project_dir: Path, phase_key: str, status: str,
                   detail: str = ..., source_hash: str | None = ...) -> None: ...
    def set_current(self, project_dir: Path, current: str) -> None: ...
    def phase_valid(self, project_dir: Path, phase_key: str,
                    current_hash: str | None = ...) -> bool: ...


@runtime_checkable
class ProcessResult(Protocol):
    """Result of a cargo/git subprocess invocation.

    Read-only members (declared as properties): a finished process is
    immutable, and read-only protocol members let a frozen-dataclass adapter
    result (`adapters.cargo.Process`) structurally satisfy the port — mutable
    `x: int` members would reject it, since frozen fields aren't settable.
    """

    @property
    def returncode(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...


@runtime_checkable
class RefineStrategy(Protocol):
    """The task-specific half of a refine loop (see `application.refine_loop`). The loop driver
    owns the iterate/stall/retro/budget shape; the strategy owns the domain work — measure the
    metric, decide success, do one generate+apply attempt (reading `state.context()` for
    meta-awareness), and produce a retrospective on stall. Implementations do the I/O
    (model calls, cargo); the loop driver and `LoopState` stay pure."""

    def measure(self) -> float:
        """Current value of the metric being improved (coverage %, pass count, -error count, …)."""
        ...

    def is_done(self, metric: float) -> bool:
        """True when the loop has succeeded and should stop (e.g. 100% / all tests pass)."""
        ...

    def attempt(self, state: LoopState) -> Attempt:
        """Run ONE generate+apply step. Read `state.context()` to put the trajectory/retro into
        the prompt. Return the resulting :class:`Attempt` (its `improved` flag is recomputed by
        the driver against the running best, so the strategy may leave it False)."""
        ...

    def retrospect(self, state: LoopState) -> str:
        """Produce retrospective guidance on a stall (or "" for none). Surfaced to the next attempt
        via `state.context()`."""
        ...
