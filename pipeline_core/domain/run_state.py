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


@dataclass(frozen=True, slots=True)
class RunPlan:
    """The parsed CLI contract (plan #77 T1, parse-don't-validate): argparse's
    Namespace is read EXACTLY ONCE — in :meth:`from_args` — and everything
    downstream consumes this frozen, typed VO. Field set = the flags the run
    loop actually consumes (display-only flags like --best-models-list stay
    in `main`)."""

    project: str = "agave"
    repo: str | None = None
    clean: bool = False
    progress: bool = False
    nolog: bool = False
    augment_tests: bool = False
    from_spec: bool = False
    restart_from_augmented: bool = False
    restart_from_pre_augment: bool = False
    audit: bool = False
    phase_a_only: bool = False
    phase_b_only: bool = False
    phase_c_only: bool = False
    phase_d_only: bool = False
    skeleton_backend: bool = False

    @classmethod
    def from_args(cls, args: object) -> RunPlan:
        """Build from an argparse Namespace (or any attribute bag — tests pass
        SimpleNamespace). Missing attributes take the field default, so a
        partial namespace never crashes the boundary."""
        from dataclasses import fields
        return cls(**{
            f.name: getattr(args, f.name)
            for f in fields(cls) if hasattr(args, f.name)
        })
