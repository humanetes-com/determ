"""Workspace lifecycle adapter, relocated out of pipeline.py.

`clean_state` wipes every pipeline-generated artefact from a project workspace so
the next run starts fresh: manifest-tracked source, per-iteration documents,
augmentation/strip/lock sentinels, the chunk cache, the coverage/stream-log scratch,
and all `gen_tests` siblings/decls. Pure filesystem mutation ⇒ an adapter; the
on-disk vocabulary comes from `domain.workspace_layout`, the gen_tests text rules
from `domain.generation`. The one collaborator with its own manifest logic
(`delete_source_files`) is injected.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pipeline_core import log as _log
from pipeline_core.adapters.state import state_path
from pipeline_core.domain.generation import (
    is_gen_tests_sibling_name,
    strip_all_gen_tests_decls,
    strip_trailing_legacy_gen_block,
)
from pipeline_core.domain.workspace_layout import (
    CHUNK_CACHE_DIR,
    CLUSTER_AUGMENTED_DIR,
    COVERAGE_SCRATCH_DIR,
    COVERAGE_STRATEGY_HASH_NAME,
    COVERAGE_STRATEGY_NAME,
    FAILURE_LOG_NAME,
    LOCKED_MODULES_DIR,
    MODULE_AUGMENTED_DIR,
    MODULE_STRIPPED_DIR,
    PROJECT_CONTEXT_NAME,
    PROJECT_INPUTS_HASH_NAME,
    PROJECT_PROFILE_NAME,
    STREAM_LOG_DIR,
)


def clean_state(
    root: Path,
    project_dir: Path,
    manifest_path: Path,
    genesis_path: Path,
    functional_req_path: Path,
    *,
    delete_source_files: Callable[[Path, Path], None],
) -> None:
    """Delete all pipeline-generated state so the next run starts fresh."""
    # Generated source files tracked in the manifest
    if manifest_path.exists():
        delete_source_files(project_dir, manifest_path)

    # Per-iteration documents, the project profile, and synthesized context (all per-project)
    profile_path       = project_dir / PROJECT_PROFILE_NAME
    context_path       = project_dir / PROJECT_CONTEXT_NAME
    hash_path          = project_dir / PROJECT_INPUTS_HASH_NAME
    strategy_path      = project_dir / COVERAGE_STRATEGY_NAME
    strategy_hash_path = project_dir / COVERAGE_STRATEGY_HASH_NAME
    state_p            = state_path(project_dir)
    for p in [functional_req_path, genesis_path, profile_path, context_path, hash_path,
              strategy_path, strategy_hash_path, state_p]:
        if p.exists():
            p.unlink()
            _log.log(f"  Deleted {p.name}")

    # Test output files
    for p in project_dir.glob("tests_output_*.txt"):
        p.unlink()
        _log.log(f"  Deleted {p.name}")

    # Chunk partials from interrupted chunked task1
    partials_dir = project_dir / ".task1_partials"
    if partials_dir.exists():
        for p in partials_dir.glob("*.txt"):
            p.unlink()
        with suppress(OSError):
            partials_dir.rmdir()
        _log.log("  Deleted .task1_partials/")

    # Failure log
    failure_log_path = project_dir / FAILURE_LOG_NAME
    if failure_log_path.exists():
        failure_log_path.unlink()
        _log.log(f"  Deleted {FAILURE_LOG_NAME}")

    # Chunk cache (persistent across iterations — only wiped on explicit --clean)
    cache_dir = root / CHUNK_CACHE_DIR
    if cache_dir.exists():
        deleted = 0
        for p in cache_dir.glob("*.txt"):
            p.unlink()
            deleted += 1
        with suppress(OSError):
            cache_dir.rmdir()
        _log.log(f"  Deleted {CHUNK_CACHE_DIR}/ ({deleted} cached chunks)")

    # Cluster augmentation sentinels — must be wiped so task0c reruns on --clean
    cluster_aug_dir = project_dir / CLUSTER_AUGMENTED_DIR
    if cluster_aug_dir.exists():
        shutil.rmtree(cluster_aug_dir)
        _log.log(f"  Deleted {CLUSTER_AUGMENTED_DIR}/")

    # Module augmentation and strip sentinels
    for _sentinel_dir_name in (MODULE_AUGMENTED_DIR, MODULE_STRIPPED_DIR):
        _sentinel_dir = project_dir / _sentinel_dir_name
        if _sentinel_dir.exists():
            shutil.rmtree(_sentinel_dir)
            _log.log(f"  Deleted {_sentinel_dir_name}/")

    # Locked module backups (bottom-up integration)
    locked_dir = project_dir / LOCKED_MODULES_DIR
    if locked_dir.exists():
        shutil.rmtree(locked_dir)
        _log.log(f"  Deleted {LOCKED_MODULES_DIR}/")

    # Per-module generated-file manifests
    for p in project_dir.glob(".generated_files_*.txt"):
        p.unlink()
        _log.log(f"  Deleted {p.name}")

    # Per-workspace scratch: coverage tool outputs and saved stream logs. They
    # used to be `/tmp/<tool>_<module>` files, and this wipe deleted them by an
    # absolute glob — the coverage files of WHATEVER run was live, since
    # `tests/test_clean_state.py` calls this function for real under every
    # `check.sh`. Scoped to this workspace, a wipe can only reach its own.
    for _rel in (COVERAGE_SCRATCH_DIR, STREAM_LOG_DIR):
        _scratch = project_dir / _rel
        if _scratch.is_dir():
            shutil.rmtree(_scratch)
            _log.log(f"  Deleted {_rel}/")

    # Strip all gen_tests artifacts from source files: delete sibling files and
    # remove path-mod declarations. Also clean up legacy inline blocks (from
    # pre-sibling-file design) so an old workspace can be salvaged with --clean.
    stripped_files = stripped_siblings = 0
    # Delete sibling files first so the parent loop doesn't re-read them.
    for sibling in sorted(project_dir.rglob("*.gen_tests_*.rs")):
        if is_gen_tests_sibling_name(sibling.name):
            try:
                sibling.unlink()
                stripped_siblings += 1
                _log.log(f"  Deleted sibling {sibling.relative_to(project_dir)}")
            except OSError:
                pass
    for src in sorted(project_dir.rglob("*.rs")):
        try:
            raw = src.read_text(encoding="utf-8")
            cleaned = strip_all_gen_tests_decls(raw)
            cleaned = strip_trailing_legacy_gen_block(cleaned).rstrip() + "\n"
            if cleaned != raw:
                src.write_text(cleaned, encoding="utf-8")
                _log.log(f"  Stripped gen_tests artifacts from {src.relative_to(project_dir)}")
                stripped_files += 1
        except Exception:
            pass
    if stripped_files or stripped_siblings:
        _log.log(f"  Removed {stripped_siblings} sibling file(s); cleaned {stripped_files} source file(s).")

    _log.log("Clean complete. Starting from scratch.")
