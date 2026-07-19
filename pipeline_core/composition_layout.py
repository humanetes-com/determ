"""Composition wiring for the F11 t1.5 layout-authoring phase.

Assembles the phase runner from the adapter-level ports (behaviour store,
private-surface store, skeleton compile-check, API surface) so pipeline.py only
names the handful of run-scoped singletons it cannot avoid owning (the model
client, the baked-cluster loader, fn-ref counts, the logger). Composition layer:
free to import adapters + application (like composition.py).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.adapters.artifacts import api_surface_path
from pipeline_core.adapters.behaviour_store import load_behaviour_suite
from pipeline_core.adapters.fn_revert import revert_fns_to_stub
from pipeline_core.adapters.layout_outcome_store import (
    load_layout_records,
    load_retrigger_attempts,
    save_layout_outcomes,
    save_retrigger_attempts,
    update_layout_outcome,
)
from pipeline_core.adapters.private_surface_store import load_private_surface
from pipeline_core.adapters.skeleton_compile import (
    compile_skeleton_check,
    skeleton_src_dir_for,
)
from pipeline_core.application.author_layout import AuthorLayoutDeps
from pipeline_core.application.author_layout_phase import make_layout_phase_runner
from pipeline_core.application.retrigger_layout import (
    RetriggerLayoutDeps,
    RetriggerResult,
    retrigger_layout,
)


def build_layout_phase_runner(
    *,
    enabled: bool,
    ask: Callable[..., str],
    task2_model: str,
    load_baked_clusters: Callable[[Path], list[Any]],
    fn_name_counts: Callable[[Path], dict[str, int]],
    log: Callable[[str], None],
) -> Callable[[Path, str], None]:
    """The t1.5 phase runner, bound to the resident adapters."""
    return make_layout_phase_runner(
        enabled=enabled,
        load_baked_clusters=load_baked_clusters,
        load_surface=load_private_surface,
        load_suite=load_behaviour_suite,
        api_surface_path=api_surface_path,
        run_model=lambda prompt: ask(prompt, model=task2_model),
        compile_check=lambda pd, c, env: compile_skeleton_check(
            pd, c, env_extra=env),
        skeleton_src_dir_for=skeleton_src_dir_for,
        fn_name_counts=fn_name_counts,
        log=log,
        save_layout_outcomes=save_layout_outcomes,
    )


def build_retrigger_layout(
    *,
    ask: Callable[..., str],
    task2_model: str,
    load_baked_clusters: Callable[[Path], list[Any]],
    enumerate_fns: Callable[[Path], list[Any] | None],
    enumerate_types: Callable[[Path], list[Any] | None],
    git_commit: Callable[[Path, str], bool],
    log: Callable[[str], None],
    max_attempts: int,
) -> Callable[[Path, str, str, Path, str], RetriggerResult]:
    """The F11 M3 layout re-trigger action, bound to the resident adapters —
    same building blocks `build_layout_phase_runner` uses (this IS t1.5,
    invoked again mid-run for one component instead of the whole surface)."""

    def _run(project_dir: Path, module_name: str, cfg_cluster: str,
              target_src_dir: Path, error_text: str,
              crash_evidence: Any = None) -> RetriggerResult:
        clusters = load_baked_clusters(project_dir) or []
        env_by_cluster = {c.name: dict(c.env_extra) for c in clusters}

        def _public_surface_text(pd: Path, cluster: str) -> str:
            try:
                p = api_surface_path(pd, module_name)
                return p.read_text(encoding="utf-8") if p.exists() else ""
            except Exception:
                return ""

        def _author_deps_for(pd: Path, cluster: str) -> AuthorLayoutDeps:
            src = skeleton_src_dir_for(pd, cluster)

            def _read(rel: str) -> str | None:
                try:
                    return (src / rel).read_text(encoding="utf-8")
                except OSError:
                    return None

            def _write(rel: str, txt: str) -> None:
                fp = src / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(txt, encoding="utf-8")

            return AuthorLayoutDeps(
                run_model=lambda prompt: ask(prompt, model=task2_model),
                read_skeleton=_read, write_skeleton=_write,
                compile_check=lambda: compile_skeleton_check(
                    pd, cluster, env_extra=env_by_cluster.get(cluster, {})),
                log=log)

        deps = RetriggerLayoutDeps(
            log=log, load_surface=load_private_surface,
            load_layout_records=load_layout_records,
            update_layout_outcome=update_layout_outcome,
            enumerate_skeleton_fns=lambda pd, c: enumerate_fns(
                skeleton_src_dir_for(pd, c)),
            enumerate_skeleton_types=lambda pd, c: enumerate_types(
                skeleton_src_dir_for(pd, c)),
            load_suite=load_behaviour_suite,
            public_surface_text=_public_surface_text,
            symbol_inventory_text=lambda pd, c: "",
            author_deps_for=_author_deps_for,
            enumerate_fns=enumerate_fns,
            revert_fns=revert_fns_to_stub,
            git_commit=git_commit,
            load_attempts=load_retrigger_attempts,
            save_attempts=save_retrigger_attempts,
            max_attempts=max_attempts,
        )
        return retrigger_layout(
            project_dir, module_name, cfg_cluster, target_src_dir, error_text,
            deps, crash_evidence=crash_evidence)

    return _run
