"""CallableCommitGate — the atomic commit-then-mark transition (P6).

Satisfies :class:`pipeline_core.ports.augment.CommitGate`. The actual
commit-and-mark logic stays at the composition root (it binds the `_git_managed`
runtime flag + the git/state primitives); this adapter is the narrow boundary the
`ClusterAugmentor` use-case depends on, wrapping that injected callable so the
use-case never reaches a god-adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class CallableCommitGate:
    def __init__(self, commit_and_mark: Callable[..., None]) -> None:
        self._commit_and_mark = commit_and_mark

    def commit_and_mark(self, project_dir: Path, phase_key: str, status: str, *,
                        commit_message: str, detail: str = "",
                        source_hash: str | None = None) -> None:
        self._commit_and_mark(project_dir, phase_key, status,
                              commit_message=commit_message, detail=detail,
                              source_hash=source_hash)
