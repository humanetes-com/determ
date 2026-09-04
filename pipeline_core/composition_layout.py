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
from pipeline_core.adapters.death_record import deliberate_stop_pids
from pipeline_core.adapters.fn_revert import revert_fns_to_stub
from pipeline_core.adapters.gen_tests_storage import (
    read_frozen_test_sources,
    read_oracle_sibling_sources,
)
from pipeline_core.adapters.layout_outcome_store import (
    load_layout_records,
    load_rejection_notes,
    load_retrigger_attempts,
    save_layout_outcomes,
    save_rejection_notes,
    save_retrigger_attempts,
    update_layout_outcome,
)
from pipeline_core.adapters.layout_registry_store import (
    load_registry,
    save_registry,
    upsert_registry,
)
from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree
from pipeline_core.adapters.private_surface_store import load_private_surface
from pipeline_core.adapters.skeleton_compile import (
    compile_skeleton_check,
    skeleton_src_dir_for,
    write_pin_probe,
)
from pipeline_core.application.author_layout import AuthorLayoutDeps
from pipeline_core.application.author_layout_phase import make_layout_phase_runner
from pipeline_core.application.reconstruct.tree_txn import TreeTxn
from pipeline_core.application.retrigger_layout import (
    RetriggerLayoutDeps,
    RetriggerResult,
    retrigger_layout,
)
from pipeline_core.domain.measurement import code_identity


def build_layout_phase_runner(
    *,
    enabled: bool,
    ask: Callable[..., str],
    task2_model: str,
    load_baked_clusters: Callable[[Path], list[Any]],
    fn_name_counts: Callable[[Path], dict[str, int]],
    log: Callable[[str], None],
    author_private_fields: bool = False,
) -> Callable[[Path, str], None]:
    """The t1.5 phase runner, bound to the resident adapters."""
    return make_layout_phase_runner(
        enabled=enabled,
        load_baked_clusters=load_baked_clusters,
        load_surface=load_private_surface,
        load_suite=load_behaviour_suite,
        api_surface_path=api_surface_path,
        run_model=lambda prompt: ask(prompt, model=task2_model),
        compile_check=lambda pd, c, cl: compile_skeleton_check(
            pd, c, cluster=cl),
        skeleton_src_dir_for=skeleton_src_dir_for,
        fn_name_counts=fn_name_counts,
        log=log,
        save_layout_outcomes=save_layout_outcomes,
        load_registry=load_registry, save_registry=save_registry,
        write_pin_probe=write_pin_probe,
        read_oracle_sources=read_oracle_sibling_sources,
        read_frozen_tests=read_frozen_test_sources,
        author_private_fields=author_private_fields,
    )


def _read_tree_file(src_dir: Path, rel: str) -> str | None:
    try:
        return (Path(src_dir) / rel).read_text(encoding="utf-8")
    except OSError:
        return None


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
    parse_ok: Callable[[Path], bool] | None = None,
) -> Callable[[Path, str, str, Path, str], RetriggerResult]:
    """The F11 M3 layout re-trigger action, bound to the resident adapters —
    same building blocks `build_layout_phase_runner` uses (this IS t1.5,
    invoked again mid-run for one component instead of the whole surface)."""
    # Taken ONCE, at composition: it stamps a mechanical-failure streak with the
    # build that earned it, and the build that earns one is the code this
    # process imported at start, not whatever is on disk when the sweep reads
    # it. (`run_phase_c_tdd` re-reads its own copy per entry on purpose — that
    # one labels a measurement, which a mid-run edit really does invalidate.)
    code_id = code_identity(Path.cwd())

    # Both live-tree writes of the retrigger are MECHANICAL — no new
    # information enters the tree — so each runs as a parse-verified
    # transaction and is undone by byte restore when it breaks a file.
    def _apply_layout_in_txn(plan: dict[str, list[Any]], src_dir: Path,
                             ) -> tuple[int, str]:
        txn = TreeTxn(src_dir, validate_parse=parse_ok)
        written, reason = apply_layout_to_tree(plan, txn)
        if reason:
            txn.rollback()
            return 0, reason
        verdict = txn.close()
        if not verdict.ok:
            log(f"  [layout-retrigger] propagation REVERTED: {verdict.reason}")
            return 0, verdict.reason
        return written, ""

    def _revert_fns_in_txn(fns: list[Any], src_dir: Path) -> int:
        txn = TreeTxn(src_dir, validate_parse=parse_ok)
        n = revert_fns_to_stub(fns, txn)
        verdict = txn.close()
        if not verdict.ok:
            log(f"  [layout-retrigger] surgical revert REVERTED: {verdict.reason}")
            return 0
        return n

    def _run(project_dir: Path, module_name: str, cfg_cluster: str,
              target_src_dir: Path, error_text: str,
              crash_evidence: Any = None,
              compile_evidence: Any = None,
              best_pass: int | None = None) -> RetriggerResult:
        clusters = load_baked_clusters(project_dir) or []
        cluster_by_name = {c.name: c for c in clusters}

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
                    pd, cluster, cluster=cluster_by_name.get(cluster)),
                log=log,
                write_pin_probe=lambda src: write_pin_probe(pd, cluster, src),
                # The re-trigger re-authors ONE component mid-run, against the
                # same oracle: a layout it invents without these paths breaks
                # the same write-protected tests the first pass had to satisfy.
                oracle_sources=lambda: read_oracle_sibling_sources(pd),
                frozen_test_sources=lambda: read_frozen_test_sources(pd))

        deps = RetriggerLayoutDeps(
            log=log, load_surface=load_private_surface,
            load_layout_records=load_layout_records,
            update_layout_outcome=update_layout_outcome,
            upsert_layout_registry=upsert_registry,
            enumerate_skeleton_fns=lambda pd, c: enumerate_fns(
                skeleton_src_dir_for(pd, c)),
            enumerate_skeleton_types=lambda pd, c: enumerate_types(
                skeleton_src_dir_for(pd, c)),
            load_suite=load_behaviour_suite,
            public_surface_text=_public_surface_text,
            symbol_inventory_text=lambda pd, c: "",
            author_deps_for=_author_deps_for,
            enumerate_fns=enumerate_fns,
            read_tree_file=lambda sd, rel: _read_tree_file(sd, rel),
            revert_fns=_revert_fns_in_txn,
            apply_layout_to_tree=_apply_layout_in_txn,
            git_commit=git_commit,
            load_attempts=load_retrigger_attempts,
            save_attempts=save_retrigger_attempts,
            load_rejection_notes=load_rejection_notes,
            save_rejection_notes=save_rejection_notes,
            max_attempts=max_attempts,
            code_id=code_id,
            deliberate_stop_pids=deliberate_stop_pids,
        )
        return retrigger_layout(
            project_dir, module_name, cfg_cluster, target_src_dir, error_text,
            deps, crash_evidence=crash_evidence,
            compile_evidence=compile_evidence, best_pass=best_pass)

    return _run
