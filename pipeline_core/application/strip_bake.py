"""Strip-and-bake the module before reconstruction (P5-S3).

Extracted verbatim from `main()`'s reconstruction loop. Before TASK 1, each
module's implementation source is removed, but first — the original bodies are
readable exactly once, here — every
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
from pipeline_core.domain.behaviour import BehaviourSuite, merge_suites
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.inline_tests import find_inline_test_mods
from pipeline_core.domain.models import (
    BuildCluster,
    FnRecord,
    ModRecord,
    SkeletoniseResult,
    TypeRecord,
    UseRecord,
)
from pipeline_core.domain.module_reachability import externally_reachable_files
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    SurfaceItemKind,
    classify_private_surface,
)
from pipeline_core.domain.rust_name_scope import ModuleScopes
from pipeline_core.domain.test_assertions import GEN_TESTS_MARKER
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES


@dataclass(frozen=True)
class BakeArtifactsDeps:
    """Collaborators for the pre-strip bake, the one place original source is read.
    All but BuildCluster
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
    # F11 M1: enumerate the skeleton's types/fns (injected extractor) + persist
    # the public-frozen vs private-replaceable marks the t1.5 phase consumes.
    enumerate_types: Callable[[Path], list[TypeRecord] | None]
    enumerate_fns: Callable[[Path], list[FnRecord] | None]
    save_private_surface: Callable[[Path, PrivateSurface], Path]
    # module-reachability fix: which mods gate which files, so a `pub` item
    # behind a private `mod` wall is classified replaceable, not frozen.
    enumerate_mods: Callable[[Path], list[ModRecord] | None]
    # `use` bindings per file, so a private name is only a reference edge where
    # the ident actually denotes it (see domain/rust_name_scope).
    enumerate_uses: Callable[[Path], list[UseRecord] | None]
    # Freeze-scope switch: may the model design the PRIVATE FIELDS of public
    # structs? Baked into the artefact (see PrivateSurface.author_private_fields)
    # because the API-surface capture is elided to match — the two are one
    # decision, taken once per workspace.
    author_private_fields: bool = False
    # The suite an EARLIER phase (t0d) already wrote for the cluster. The bake
    # re-harvests from the same source, so its ids coincide with t0d's; what
    # only t0d can produce — probe specs with a recorded stdout, `exercises`
    # filled by coverage — must survive the bake, so the bake MERGES (existing
    # wins) instead of overwriting. Default: nothing earlier (a fresh bake).
    load_behaviour_suite: Callable[[Path, str], BehaviourSuite] = (
        lambda _p, c: BehaviourSuite(cfg_cluster=c))


def _behaviour_sources(src_dir: Path) -> list[tuple[str, str]]:
    """(`rel_path`, `text`) for each crate source file whose doc-comments may
    carry behaviours — skipping build/aux dirs and the pipeline's own
    generated test siblings (`*.gen_tests_*.rs` were authored against the
    private layout; a behaviour lifted from one pins the original's
    internals). Read once, pre-strip."""
    out: list[tuple[str, str]] = []
    for rs in sorted(src_dir.rglob("*.rs")):
        if any(part in SKIP_DIR_NAMES for part in rs.parts):
            continue
        if GEN_TESTS_MARKER in rs.name:
            continue
        try:
            out.append((str(rs.relative_to(src_dir)),
                        rs.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def _inline_test_mods_left(skel_src: Path) -> list[str]:
    """`file::mod` for every inline test module still present in the skeleton."""
    out: list[str] = []
    for rs in sorted(skel_src.rglob("*.rs")):
        try:
            text = rs.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(f"{rs.name}::{b.name}" for b in find_inline_test_mods(text))
    return out


def _test_layout_sources(project_dir: Path) -> list[tuple[str, str]]:
    """(`rel_path`, `text`) for each integration test file under ``tests/``.

    The layout pins F11's t1.5 must satisfy (`size_of`, NPO, autotraits) live in
    these test BODIES, not in doc-fences (M12). Read once, pre-strip — a
    `size_of` assertion is an observable fact, the same legality class as any
    harvested test assertion, so reading it pre-strip is legitimate."""
    tests_dir = project_dir / "tests"
    out: list[tuple[str, str]] = []
    if not tests_dir.is_dir():
        return out
    for rs in sorted(tests_dir.rglob("*.rs")):
        if any(part in SKIP_DIR_NAMES for part in rs.parts):
            continue
        if GEN_TESTS_MARKER in rs.name:
            continue
        try:
            out.append((str(rs.relative_to(project_dir)),
                        rs.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def bake_skeleton_artifacts(
    project_dir: Path, module_name: str, deps: BakeArtifactsDeps,
) -> dict[str, Any]:
    """Run BEFORE strip while live source is on disk — the ONLY place pipeline
    code may read the original bodies. Discovers + prunes clusters,
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
        _stranded = _inline_test_mods_left(skeleton_dir / "src")
        if _stranded:
            # The skeletoniser keeps `#[cfg(test)] mod` deliberately (dropping
            # it would lose the oracle), so anything still inline here escaped
            # the preservation step — and will be handed to the fill routes as
            # writable text. Name it: a silently-inline test reads later as a
            # passing test that asserts nothing.
            d.log(f"  [bake-skeleton] cluster {cluster.name}: WARNING — "
                  f"{len(_stranded)} inline test mod(s) survived into the "
                  f"skeleton: {', '.join(_stranded)}. They remain writable by "
                  "the fill routes.")
        # F11 M1: classify the skeleton's private layout (public-frozen vs
        # private-replaceable) so the t1.5 phase knows what it may re-author.
        # Visibility is an observable structural fact, safe to use post-strip.
        # Additive: a classification failure never blocks the strip.
        try:
            _skel_src = skeleton_dir / "src"
            types = d.enumerate_types(_skel_src) or []
            fns = d.enumerate_fns(_skel_src) or []
            mods = d.enumerate_mods(_skel_src) or []
            reachable = externally_reachable_files(
                list(mods), frozenset(t.file for t in types))
            # `None` = the extractor could not report `use` bindings at all (a
            # stale binary). That is NOT the same as a crate with no imports:
            # resolving names against an empty binding set would silently drop
            # every cross-file edge, so fall back to bare-name matching and say
            # so, rather than shipping a fragmented component partition.
            uses = d.enumerate_uses(_skel_src)
            if uses is None:
                d.log(f"  [bake-skeleton] cluster {cluster.name}: no `use` "
                      "bindings available — private-surface names resolved "
                      "crate-globally (run `bash setup.sh` to rebuild the "
                      "extractor).")
            scopes = (None if uses is None
                      else ModuleScopes.from_records(list(mods), list(uses)))
            surface = classify_private_surface(
                list(types), list(fns), cfg_cluster=cluster.name,
                cfg_set=CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false)),
                reachable_files=reachable, scopes=scopes,
                author_private_fields=d.author_private_fields)
            d.save_private_surface(project_dir, surface)
            _pub_fields = [i.name for i in surface.replaceable
                           if i.kind is SurfaceItemKind.PUB_FIELDS]
            if _pub_fields:
                d.log(f"  [bake-skeleton] cluster {cluster.name}: public struct(s) "
                      f"whose private fields are model-authored: "
                      f"{', '.join(sorted(_pub_fields))}")
            d.log(f"  [bake-skeleton] cluster {cluster.name}: private surface — "
                  f"{len(surface.replaceable)} replaceable item(s) in "
                  f"{len(surface.components)} component(s), "
                  f"{len(surface.frozen_public)} frozen-public, "
                  f"largest component {max((len(c) for c in surface.components), default=0)}.")
        except Exception as e:
            d.log(f"  [bake-skeleton] cluster {cluster.name}: private-surface "
                  f"classification failed (non-fatal): {e}")
        # F10 t0d: harvest this cfg-cluster's behaviour oracle from live source
        # (pre-strip — behaviours are OBSERVABLE outcomes, the same
        # legality class as a test assertion, never bodies). Additive: a harvest
        # failure never blocks the strip.
        try:
            sources = _behaviour_sources(src_dir)
            test_sources = _test_layout_sources(project_dir)
            _default_name = next((c.name for c in clusters if c.is_default),
                                 cluster.name)
            suite = harvest_behaviour_suite(
                sources, cfg_cluster=cluster.name, test_sources=test_sources,
                default_cluster=_default_name)
            # Never overwrite what t0d wrote: same ids → same spec, and the
            # probe/exercises data only t0d has is kept (existing wins).
            earlier = d.load_behaviour_suite(project_dir, cluster.name)
            merged = merge_suites(earlier, suite)
            if merged.specs:
                d.save_behaviour_suite(project_dir, merged)
                d.log(f"  [bake-skeleton] cluster {cluster.name}: harvested "
                      f"{len(suite.specs)} behaviour(s), {len(earlier.specs)} "
                      f"already on disk from an earlier phase → "
                      f"{len(merged.specs)} in "
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
    # F11 t1.5 (M2): re-author each cluster's private layout over the baked
    # skeleton, BEFORE the augmentation commit so the rewrite is captured. A
    # no-op binding disables the phase; any failure inside it degrades to the
    # frozen skeleton (the run proceeds exactly as before F11).
    run_layout_phase: Callable[[Path, str], None] = lambda _p, _m: None
    # Strip rewrites mod declarations (drops `#[cfg(test)] mod`) under the
    # same src path earlier phases already queried — cached mod records
    # would misclassify cluster membership afterwards.
    invalidate_structure: Callable[[], None] = lambda: None
    # Tests written INLINE in a module file sit in the same text the fill routes
    # show the model and splice back; relocate them into gen_tests siblings
    # before ANY artefact is derived from source, so no later phase can offer a
    # test body as writable surface. Takes the MODULE's crate root (the dir
    # holding this module's src/ — the member dir in a workspace, not the
    # workspace root, or the step scans a src/ that does not exist and silently
    # relocates nothing). A no-op disables it.
    # The result is the caller's own report (counts + refusals, already logged);
    # this step only needs the relocation to have happened.
    preserve_inline_tests: Callable[[Path], object] = lambda _p: None


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
            _strip_cov = float(_sentinel_path.read_text().strip())  # numeric sentinel
        except (ValueError, OSError):
            _strip_cov = 0.0
        if _strip_cov < d.coverage_target:
            d.log(
                f"\n  WARNING at strip: '{module_name}' coverage is {_strip_cov:.1f}% "
                f"— stripping anyway. Reconstruction may be incomplete."
            )
    # Move any inline `#[cfg(test)] mod` into a gen_tests sibling FIRST, so the
    # API snapshot and the skeleton below are both derived from the canonical
    # shape (production code only) rather than from a file that still carries
    # its own oracle.
    d.preserve_inline_tests(module_dir)
    # Capture API surface BEFORE strip — structural ground truth for
    # TASK 1. The .original_structure/ dir survives strip (lives at the
    # project root, not under src/), so TASK 1 can read it post-strip.
    d.capture_api_surface(project_dir, module_dir, module_name)
    # Bake the skeleton + leaf-first ordering +
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
    # F11 t1.5: author the private layout over the baked skeleton BEFORE the
    # augmentation commit below (so the rewrite is committed with the skeleton).
    # Degrade-safe: a failure here leaves the frozen skeleton untouched.
    try:
        d.run_layout_phase(project_dir, module_name)
    except Exception as e:
        d.log(f"  [t1.5] layout-authoring phase failed (non-fatal): {e}")
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
    d.invalidate_structure()
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
