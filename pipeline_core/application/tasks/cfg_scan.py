"""TASK 0 cfg scan — derive compiler clusters from source (use-case).

Drives the syn extractor (via :class:`pipeline_core.ports.cluster_facts.ClusterFactReader`)
for the `#[cfg]` facts, parses `Cargo.toml` features (domain), and hands both to the
pure :mod:`pipeline_core.domain.cluster_plan` planner. No regex on source — the
predicate scan that used to live in ``pipeline.task0_cfg_scan`` is gone.

I/O here is read-only (extractor + Cargo.toml); writing ``build_configs.md`` stays
with the caller, which is why ``scan_build_clusters`` also returns the predicate
counts (the report shows per-cluster gated-line totals).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from ...domain import cluster_plan
from ...domain.build_script import BuildScriptCfg
from ...domain.cargo_toml import load_cargo_toml
from ...domain.generation import is_gen_tests_sibling_name
from ...domain.models import BuildCluster
from ...log import log
from ...ports.build_script import BuildScriptReader
from ...ports.cluster_facts import ClusterFactReader


class ClusterScan(NamedTuple):
    clusters: list[BuildCluster]
    predicate_counts: dict[str, int]


def _build_script_facts(
    project_dir: Path, build_script: BuildScriptReader | None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(set-under-default, set-only-under-nightly, declared) cfgs from `build.rs`,
    captured under the available toolchains. Empty when no reader / no build script
    — the planner then keeps every custom atom (the old, pre-pruning behaviour)."""
    if build_script is None:
        return frozenset(), frozenset(), frozenset()
    default = build_script.emitted_cfgs(project_dir, None) or BuildScriptCfg.empty()
    nightly = build_script.emitted_cfgs(project_dir, "nightly") or BuildScriptCfg.empty()
    set_default = default.set_cfgs
    set_nightly = nightly.set_cfgs - set_default
    declared = default.declared_cfgs | nightly.declared_cfgs
    return set_default, set_nightly, declared


def _cargo_features(project_dir: Path) -> tuple[set[str], set[str]]:
    """(all declared feature names incl. ``default``, default-enabled features)."""
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return set(), set()
    feats = load_cargo_toml(cargo_toml.read_text(errors="replace")).get("features", {})
    if not isinstance(feats, dict):
        return set(), set()
    default = feats.get("default", [])
    default_set = {str(x) for x in default} if isinstance(default, list) else set()
    return set(feats.keys()), default_set


def scan_build_clusters(project_dir: Path, reader: ClusterFactReader,
                        build_script: BuildScriptReader | None = None) -> ClusterScan:
    """Derive the cluster plan for ``project_dir`` from its source cfg facts,
    pruned by what ``build.rs`` actually emits when a ``build_script`` reader
    is supplied (else every custom cfg atom is kept — the old, pre-pruning behaviour)."""
    src_dir = project_dir / "src"
    cargo_features, default_features = _cargo_features(project_dir)

    # Cluster identity is derived from REAL source directives only.
    # `*.gen_tests_<N>.rs` siblings are LLM-authored augmented tests, not source —
    # a stray `#[cfg(not(anyhow_no_core_error))]` in one spawned a phantom cluster
    # whose harness never compiled (0% coverage → blind reconstruction). Drop them.
    occurrences = [
        occ for occ in (reader.enumerate_cfgs(src_dir) or [])
        if not is_gen_tests_sibling_name(Path(occ.file).name)
    ]
    mod_records = reader.enumerate_mods(src_dir) or []
    feature_gate_cfgs = set(reader.enumerate_feature_gate_cfgs(src_dir) or [])
    bs_set_default, bs_set_nightly, bs_declared = _build_script_facts(project_dir, build_script)

    counts = cluster_plan.predicate_counts(occurrences)
    gates = cluster_plan.mod_gate_map(mod_records)
    clusters = cluster_plan.plan_clusters(
        counts, gates, cargo_features, default_features, feature_gate_cfgs,
        bs_set_default=bs_set_default, bs_set_nightly=bs_set_nightly, bs_declared=bs_declared)

    for c in clusters:
        if c.requires_nightly:
            log(f"  cluster/{c.name}: requires nightly toolchain "
                f"(active cfg gates #![feature(...)]).")

    return ClusterScan(clusters=clusters, predicate_counts=counts)
