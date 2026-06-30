"""Crate display name — the human-facing crate label derived from a workspace
directory name. Pure (stdlib only): strips the VCS-snapshot suffix the cloner
appends (``anyhow-master`` → ``anyhow``) so logs and the from-scratch disclaimer
show the bare crate name."""
from __future__ import annotations

# Suffixes the `--repo` cloner appends when it snapshots a default branch.
_SNAPSHOT_SUFFIXES: tuple[str, ...] = ("-master", "-main", "-trunk")


def crate_display_name(project_dir_name: str) -> str:
    """``"anyhow-master"`` → ``"anyhow"``. Returns the name unchanged when it
    carries no known snapshot suffix."""
    for suffix in _SNAPSHOT_SUFFIXES:
        if project_dir_name.endswith(suffix):
            return project_dir_name[: -len(suffix)]
    return project_dir_name
