"""TASK 0 cfg scan — derive compiler clusters from source (use-case, Hard Rule 8).

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
from ...domain.cargo_toml import load_cargo_toml
from ...domain.models import BuildCluster
from ...log import log
from ...ports.cluster_facts import ClusterFactReader


class ClusterScan(NamedTuple):
    clusters: list[BuildCluster]
    predicate_counts: dict[str, int]


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


def scan_build_clusters(project_dir: Path, reader: ClusterFactReader) -> ClusterScan:
    """Derive the cluster plan for ``project_dir`` from its source cfg facts."""
    src_dir = project_dir / "src"
    cargo_features, default_features = _cargo_features(project_dir)

    occurrences = reader.enumerate_cfgs(src_dir) or []
    mod_records = reader.enumerate_mods(src_dir) or []
    feature_gate_cfgs = set(reader.enumerate_feature_gate_cfgs(src_dir) or [])

    counts = cluster_plan.predicate_counts(occurrences)
    gates = cluster_plan.mod_gate_map(mod_records)
    clusters = cluster_plan.plan_clusters(
        counts, gates, cargo_features, default_features, feature_gate_cfgs)

    for c in clusters:
        if c.requires_nightly:
            log(f"  cluster/{c.name}: requires nightly toolchain "
                f"(active cfg gates #![feature(...)]).")

    return ClusterScan(clusters=clusters, predicate_counts=counts)
