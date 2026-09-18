"""Cluster-pruning adapter, relocated out of pipeline.py.

Gathers the workspace facts the pure pruner needs — Cargo.toml ``[features].default``
and per-predicate ``#[cfg]`` gated-item counts across ``src/`` — then delegates the
keep/drop decision to :func:`pipeline_core.domain.cluster_plan.prune_redundant_clusters`.
Filesystem-reading ⇒ an adapter; the cfg predicate scan goes through the structured
``domain.cfg`` scanner (no regex on source).
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.cfg import gated_predicate_names
from pipeline_core.domain.cluster_plan import prune_redundant_clusters as _decide
from pipeline_core.domain.models import BuildCluster


def prune_redundant_clusters(
    clusters: list[BuildCluster],
    source_dir: Path,
    cargo_toml_path: Path | None = None,
    min_gated_items: int = 5,
) -> list[BuildCluster]:
    """Drop clusters whose work is already covered by another cluster.

    Reads Cargo.toml's default features and counts ``#[cfg(...)]``-gated items
    across ``source_dir/src``, then applies the pure (P)/(Q) pruning rules in
    :func:`pipeline_core.domain.cluster_plan.prune_redundant_clusters`.
    """
    if not clusters:
        return clusters
    # Parse Cargo.toml [features].default to get baseline-enabled features.
    default_features: set[str] = set()
    if cargo_toml_path is None:
        cargo_toml_path = source_dir / "Cargo.toml"
    if cargo_toml_path.exists():
        default_features = set(cargo_default_features(cargo_toml_path.read_text(errors="replace")))

    # Count per-predicate gated items in source.
    pred_counts: dict[str, int] = {}
    src_dir = source_dir / "src"
    if src_dir.exists():
        skip = frozenset({"any", "all", "not", "cfg", "cfg_attr", "feature"})
        for rs in src_dir.rglob("*.rs"):
            try:
                txt = rs.read_text(errors="replace")
            except Exception:
                continue
            for name in gated_predicate_names(txt, skip):
                pred_counts[name] = pred_counts.get(name, 0) + 1

    return _decide(clusters, default_features, pred_counts, min_gated_items=min_gated_items)
