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
from pipeline_core.adapters.private_surface_store import load_private_surface
from pipeline_core.adapters.skeleton_compile import (
    compile_skeleton_ok,
    skeleton_src_dir_for,
)
from pipeline_core.application.author_layout_phase import make_layout_phase_runner


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
        compile_ok=lambda pd, c, env: compile_skeleton_ok(pd, c, env_extra=env),
        skeleton_src_dir_for=skeleton_src_dir_for,
        fn_name_counts=fn_name_counts,
        log=log,
    )
