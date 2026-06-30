"""RunState — the explicit per-run state set once at startup and read by the
cross-cutting sinks (log, commit gate, flow auditor).

Replaces the scattered mutable ``pipeline.py`` module globals (``_git_managed``,
``_AUDIT_ENABLED``/``_AUDIT_PROJECT_DIR``, ``_log_path``/``_nolog``/
``_progress_mode``, ``_RUN_STARTED_ISO``) with one typed holder injected like
:class:`ClusterScope`. Deliberately mutable: ``init_log``/``main`` populate it at
startup and lower layers read it at call time. Stdlib only, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunState:
    """Per-run flags + paths established at startup."""

    git_managed: bool = False
    audit_enabled: bool = False
    audit_project_dir: Path | None = None
    log_path: Path | None = None
    nolog: bool = False
    progress_mode: bool = False
    run_started_iso: str = ""
