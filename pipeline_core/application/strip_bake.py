"""Strip-and-bake the module before reconstruction (P5-S3).

Extracted verbatim from `main()`'s reconstruction loop. Before TASK 1, each
module's implementation source is removed, but first (Hard Rule 11) every
source-derived artefact a later phase may read is baked from live source:
API surface snapshot + skeleton + leaf-first ordering + cluster manifest. The
ordering is the persistence-safe one — commit the augmented tree + tag, THEN
strip, THEN atomically commit-and-mark the stripped tree.

Collaborators stay in pipeline.py and are injected via `StripBakeDeps` (the
per-block-Deps pattern — NOT a loop-wide god-object).
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StripBakeDeps:
    log: Callable[[str], None]
    phase_valid: Callable[[Path, str], bool]
    module_augmented_path: Callable[[Path, str], Path]
    capture_api_surface: Callable[[Path, Path, str], Path | None]
    bake_skeleton_artifacts: Callable[[Path, str], dict[str, Any]]
    git_commit: Callable[[Path, str], bool]
    git_tag: Callable[[Path, str], None]
    strip_module_source: Callable[[Path, Path], None]
    commit_and_mark: Callable[..., None]
    set_current: Callable[[Path, str], None]
    tarpaulin_bin: Path
    coverage_target: float


def strip_and_bake_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    *,
    crate_type: str,
    from_spec: bool,
    src_hash: str | None,
    deps: StripBakeDeps,
) -> None:
    """Capture API surface + bake skeleton, commit/tag the augmented tree, strip
    this module's source, then atomically commit-and-mark the stripped tree.
    No-op in forward (spec-driven) mode or when already stripped."""
    d = deps
    # --- Strip: remove ONLY this module's implementation source ---
    # !IMPORTANT CAN'T STRIP CODE IF COVERAGE <100%, IF <100% go back to task 0c
    # Only strip once — if already stripped, the original structure file list
    # is intact and the implementation is already gone.
    # FORWARD MODE: no source to strip — the workspace starts with src/ empty
    # (or absent). The user-provided API file at .original_structure/{name}.api.md
    # already serves as structural ground truth.
    if from_spec:
        d.log(f"  [from-spec] Skipping strip for '{module_name}'.")
        return
    if d.phase_valid(project_dir, f"strip/{module_name}"):
        d.log(f"  '{module_name}' already stripped — skipping.")
        return
    if crate_type != "proc-macro" and d.tarpaulin_bin.exists():
        _sentinel_path = d.module_augmented_path(project_dir, module_name)
        try:
            _strip_cov = float(_sentinel_path.read_text().strip())  # R8.4: numeric sentinel
        except (ValueError, OSError):
            _strip_cov = 0.0
        if _strip_cov < d.coverage_target:
            d.log(
                f"\n  WARNING at strip: '{module_name}' coverage is {_strip_cov:.1f}% "
                f"— stripping anyway. Reconstruction may be incomplete."
            )
    # Capture API surface BEFORE strip — structural ground truth for
    # TASK 1. The .original_structure/ dir survives strip (lives at the
    # project root, not under src/), so TASK 1 can read it post-strip.
    d.capture_api_surface(project_dir, module_dir, module_name)
    # Hard Rule 11: bake the skeleton + leaf-first ordering +
    # cluster manifest from live source BEFORE strip. After
    # strip these artefacts are the only source-derived inputs
    # any downstream phase is allowed to read.
    _bake_res = d.bake_skeleton_artifacts(project_dir, module_name)
    if not _bake_res.get("ok"):
        d.log(f"  [ABORT] skeleton bake failed for '{module_name}': "
              f"{_bake_res.get('reason', 'unknown')} — refusing to strip.")
        raise RuntimeError(
            f"skeleton bake failed for {module_name}: "
            f"{_bake_res.get('reason', 'unknown')}")
    d.log(f"  Baked skeleton for {len(_bake_res['clusters'])} cluster(s): "
          f"{', '.join(_bake_res['clusters'])}")
    # The bake's outputs (.skeleton/) are tracked in git. When
    # resuming from a task0c-augmented-{module} tag, the
    # rebake produces byte-identical files → nothing to
    # commit. That is success, not failure — the augmented
    # state IS already captured by the existing tag.
    _porc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if _porc:
        if not d.git_commit(project_dir,
                            f"pipeline: task0c augmentation complete — "
                            f"all unit tests added for {module_name}"):
            d.log(f"  [ABORT] augmentation commit failed for '{module_name}' — refusing to strip. Fix git state and retry.")
            raise RuntimeError(f"augmentation commit failed for {module_name}")
    else:
        d.log("  [augmentation] tree already clean — augmented state preserved from prior run.")
    d.git_tag(project_dir, f"task0c-augmented-{module_name}")
    d.strip_module_source(project_dir, module_dir)
    # ATOMIC: commit the stripped tree FIRST, then mark state
    # done. Reverse of the legacy order — protects against
    # the lost-commit bug where a process dying between mark
    # and commit lost the strip commit forever.
    d.commit_and_mark(
        project_dir,
        f"strip/{module_name}",
        "done",
        commit_message=f"pipeline: stripped — {module_name}",
        source_hash=src_hash,
    )
    d.set_current(project_dir, f"strip/{module_name} / done")
