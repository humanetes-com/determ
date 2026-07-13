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

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.domain.behaviour import BehaviourSuite
from pipeline_core.domain.models import BuildCluster, SkeletoniseResult
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES


@dataclass(frozen=True)
class BakeArtifactsDeps:
    """Collaborators for the pre-strip bake (Hard Rule 11). All but BuildCluster
    are pipeline-resident (cfg scan, cluster pruning, extractor) and injected."""
    log: Callable[[str], None]
    cfg_scan: Callable[[Path], list[BuildCluster]]
    prune_clusters: Callable[..., list[BuildCluster]]
    fn_name_counts: Callable[[Path], dict[str, int]]
    extract_skeleton: Callable[..., SkeletoniseResult]
    is_gen_tests_file: Callable[[str], bool]
    gen_tests_origin_cfgs: Callable[[str], dict[int, str]]
    # F10: persist a harvested behaviour suite (adapter, injected — keeps this
    # application module off the adapters layer). Returns the written path.
    save_behaviour_suite: Callable[[Path, BehaviourSuite], Path]


def _behaviour_sources(src_dir: Path) -> list[tuple[str, str]]:
    """(`rel_path`, `text`) for each crate source file whose doc-comments may
    carry behaviours — skipping build/aux dirs. Read once, pre-strip."""
    out: list[tuple[str, str]] = []
    for rs in sorted(src_dir.rglob("*.rs")):
        if any(part in SKIP_DIR_NAMES for part in rs.parts):
            continue
        try:
            out.append((str(rs.relative_to(src_dir)),
                        rs.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def bake_skeleton_artifacts(
    project_dir: Path, module_name: str, deps: BakeArtifactsDeps,
) -> dict[str, Any]:
    """Run BEFORE strip while live source is on disk (Hard Rule 11 — the ONLY
    place pipeline code reads original source). Discovers + prunes clusters,
    then per cluster extracts the skeleton (bodies → `unimplemented!()`) to
    `.skeleton/{cluster}/` and writes leaf-first ref counts; saves the cluster
    manifest + gen_tests origin-cfg manifest for post-strip rehydration.
    Returns {'ok', 'clusters', 'reason'}."""
    d = deps
    skel_root = project_dir / ".skeleton"
    skel_root.mkdir(parents=True, exist_ok=True)

    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        return {"ok": False, "clusters": [],
                "reason": f"no source at {src_dir} — strip already happened?"}

    try:
        clusters = d.cfg_scan(project_dir)
    except Exception as e:
        return {"ok": False, "clusters": [], "reason": f"task0_cfg_scan: {e}"}
    if not clusters:
        clusters = [BuildCluster(
            name="default", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
            is_default=True,
        )]
    clusters = sorted(clusters, key=lambda c: c.name)
    d.log(f"  [bake-skeleton] discovered {len(clusters)} cluster(s): "
          f"{', '.join(c.name for c in clusters)}")

    cargo_toml = project_dir / "Cargo.toml"
    clusters = d.prune_clusters(
        clusters, project_dir,
        cargo_toml_path=cargo_toml if cargo_toml.exists() else None,
    )
    d.log(f"  [bake-skeleton] processing {len(clusters)} cluster(s) after pruning: "
          f"{', '.join(c.name for c in clusters)}")

    name_counts = d.fn_name_counts(src_dir)
    d.log(f"  [bake-skeleton] computed reference counts for {len(name_counts)} fn name(s).")

    baked: list[str] = []
    for cluster in clusters:
        skeleton_dir = skel_root / cluster.name
        if skeleton_dir.exists():
            shutil.rmtree(skeleton_dir)
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        # Pass `src_dir` (NOT project_dir) so the extractor can never walk into
        # prior cluster outputs under `.skeleton/`. Output mirrors under
        # `skeleton_dir/src/`, matching the runnable-crate layout Phase A expects.
        res = d.extract_skeleton(src_dir, skeleton_dir / "src", cluster=None)
        if not res.ok:
            d.log(f"  [bake-skeleton] cluster {cluster.name}: extractor FAILED: "
                  f"{res.stderr}")
            return {"ok": False, "clusters": baked,
                    "reason": f"extract {cluster.name}: {res.stderr}"}
        order_path = skeleton_dir / "_leaf_order.json"
        try:
            order_path.write_text(json.dumps(name_counts, sort_keys=True), encoding="utf-8")
        except Exception as e:
            d.log(f"  [bake-skeleton] cluster {cluster.name}: leaf-order write failed: {e}")
        d.log(f"  [bake-skeleton] cluster {cluster.name}: {len(res.reports)} "
              f"file(s) skeletonised → {skeleton_dir}")
        # F10 t0d: harvest this cfg-cluster's behaviour oracle from live source
        # (pre-strip, Hard Rule 11 — behaviours are OBSERVABLE outcomes, the same
        # legality class as a test assertion, never bodies). Additive: a harvest
        # failure never blocks the strip.
        try:
            sources = _behaviour_sources(src_dir)
            suite = harvest_behaviour_suite(sources, cfg_cluster=cluster.name)
            if suite.specs:
                d.save_behaviour_suite(project_dir, suite)
                d.log(f"  [bake-skeleton] cluster {cluster.name}: harvested "
                      f"{len(suite.specs)} behaviour(s) → "
                      f".skeleton/_behaviours_{cluster.name}.json")
        except Exception as e:
            d.log(f"  [bake-skeleton] cluster {cluster.name}: behaviour harvest "
                  f"failed (non-fatal): {e}")
        baked.append(cluster.name)

    try:
        manifest = {
            "module": module_name,
            "clusters": [
                {
                    "name": c.name,
                    "env_extra": dict(c.env_extra),
                    "tarpaulin_exclude_files": list(c.tarpaulin_exclude_files),
                    "excluded_cfg": list(c.excluded_cfg),
                    "active_cfg": list(c.active_cfg),
                    "prompt_nightly_rule": c.prompt_nightly_rule,
                    "is_default": c.is_default,
                    "cfg_true": list(getattr(c, "cfg_true", []) or []),
                    "cfg_false": list(getattr(c, "cfg_false", []) or []),
                    "toolchain": getattr(c, "toolchain", None),
                    "requires_nightly": bool(getattr(c, "requires_nightly", False)),
                }
                for c in clusters
            ],
        }
        (skel_root / "_clusters.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as e:
        d.log(f"  [bake-skeleton] cluster manifest write failed: {e}")

    # Capture each gen_tests sibling's ORIGIN cluster cfg from the live (pre-strip)
    # decls — strip removes the decl and keeps only the body, losing provenance.
    # Without this, downstream propagation re-derives the gate from the CURRENT
    # cluster (always-active → no protection) and a nightly-only test leaks into
    # stable/no_core → E0658. Keyed by "{parent_stem}.gen_tests_{n}" → origin cfg.
    try:
        origin: dict[str, str] = {}
        for rs in sorted(src_dir.rglob("*.rs")):
            if d.is_gen_tests_file(rs.name):
                continue
            try:
                text = rs.read_text(encoding="utf-8")
            except OSError:
                continue
            for n, cfg in d.gen_tests_origin_cfgs(text).items():
                origin[f"{rs.stem}.gen_tests_{n}"] = cfg
        (skel_root / "_gen_tests_origin.json").write_text(
            json.dumps(origin, indent=2, sort_keys=True), encoding="utf-8")
        if origin:
            d.log(f"  [bake-skeleton] captured origin cfg for {len(origin)} gen_tests sibling(s).")
    except Exception as e:
        d.log(f"  [bake-skeleton] gen_tests origin manifest write failed: {e}")

    return {"ok": True, "clusters": baked, "reason": ""}


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
