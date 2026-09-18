"""CallableCommitGate — the atomic commit-then-mark transition.

Satisfies :class:`pipeline_core.ports.augment.CommitGate`. The actual
commit-and-mark logic stays at the composition root (it binds the `_git_managed`
runtime flag + the git/state primitives); this adapter is the narrow boundary the
`ClusterAugmentor` use-case depends on, wrapping that injected callable so the
use-case never reaches a god-adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def commit_and_mark(
    project_dir: Path,
    phase_key: str,
    status: str,
    *,
    commit_message: str,
    tag: str | None = None,
    detail: str = "",
    source_hash: str | None = None,
    git_commit: Callable[[Path, str], bool],
    git_tag: Callable[[Path, str], None],
    mark_phase: Callable[..., None],
) -> None:
    """Atomic transition: COMMIT the workspace, optionally TAG, THEN mark the
    phase done. The order is the persistence fix — committing AFTER marking leaks
    irrecoverably if the process dies between mark and commit (the next run sees
    `done`, skips the phase, and the mutation is never preserved). Commit-first is
    safe: death before the mark just re-runs the idempotent phase, and the second
    commit no-ops on the already-clean tree. The git/state primitives are
    injected (composition root binds the live `_RUN.git_managed`)."""
    git_commit(project_dir, commit_message)
    if tag:
        git_tag(project_dir, tag)
    mark_phase(project_dir, phase_key, status, detail=detail, source_hash=source_hash)


class CallableCommitGate:
    def __init__(self, commit_and_mark: Callable[..., None]) -> None:
        self._commit_and_mark = commit_and_mark

    def commit_and_mark(self, project_dir: Path, phase_key: str, status: str, *,
                        commit_message: str, detail: str = "",
                        source_hash: str | None = None) -> None:
        self._commit_and_mark(project_dir, phase_key, status,
                              commit_message=commit_message, detail=detail,
                              source_hash=source_hash)
