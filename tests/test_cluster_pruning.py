"""Pure-domain characterization for `cluster_plan.prune_redundant_clusters`
(relocation of `pipeline._prune_redundant_clusters`).

The I/O fact-gathering (Cargo.toml + `src/` cfg counts) lives in the adapter
`adapters.cluster_pruning` and is exercised end-to-end by
`tests/test_pipeline_smoke.py::test_prune_redundant_clusters`. Here we pin the
pure (P)/(Q) decision logic with hand-built facts — no filesystem.
"""
from __future__ import annotations

from pipeline_core.domain.cluster_plan import prune_redundant_clusters
from pipeline_core.domain.models import BuildCluster


def _cluster(name: str, *, active: list[str], is_default: bool = False) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=active, prompt_nightly_rule="", is_default=is_default)


def test_empty_input_returns_empty() -> None:
    assert prune_redundant_clusters([], set(), {}) == []


def test_keeps_all_when_no_redundancy() -> None:
    base = _cluster("stable", active=[], is_default=True)
    feat = _cluster("async", active=["async"])
    kept = prune_redundant_clusters([base, feat], set(), {"async": 10})
    assert {c.name for c in kept} == {"stable", "async"}


def test_p_drops_cluster_whose_cfgs_are_default_features() -> None:
    # (P) active cfgs ⊆ default features → covered by baseline → drop.
    base = _cluster("stable", active=[], is_default=True)
    std = _cluster("std", active=["std"])
    kept = prune_redundant_clusters([base, std], {"std"}, {"std": 99})
    assert [c.name for c in kept] == ["stable"]


def test_q_drops_cluster_gating_too_few_items() -> None:
    # (Q) distinguishing predicate gates < min_gated_items → drop.
    base = _cluster("stable", active=[], is_default=True)
    tiny = _cluster("tiny", active=["tiny"])
    kept = prune_redundant_clusters([base, tiny], set(), {"tiny": 2}, min_gated_items=5)
    assert [c.name for c in kept] == ["stable"]


def test_q_keeps_cluster_at_threshold() -> None:
    base = _cluster("stable", active=[], is_default=True)
    big = _cluster("big", active=["big"])
    kept = prune_redundant_clusters([base, big], set(), {"big": 5}, min_gated_items=5)
    assert {c.name for c in kept} == {"stable", "big"}


def test_no_baseline_suppresses_drops() -> None:
    # No default/empty cluster establishes a baseline → drop guards never fire,
    # so even redundant-looking clusters are all kept (never strip to nothing).
    a = _cluster("a", active=["a"])
    b = _cluster("b", active=["b"])
    kept = prune_redundant_clusters([a, b], {"a", "b"}, {"a": 0, "b": 0})
    assert {c.name for c in kept} == {"a", "b"}
