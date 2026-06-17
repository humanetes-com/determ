"""Unit tests for ClusterScope (P6 engine-core keystone) — the explicit run-state
that replaced the `_current_cluster`/`_all_clusters` module globals."""
from __future__ import annotations

from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.models import BuildCluster


def _cluster(name: str) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def test_defaults_empty() -> None:
    s = ClusterScope()
    assert s.active is None and s.all == [] and s.get_active() is None


def test_set_active_and_getter_track() -> None:
    s = ClusterScope()
    c = _cluster("stable")
    s.set_active(c)
    assert s.active is c and s.get_active() is c
    s.set_active(None)
    assert s.active is None and s.get_active() is None


def test_reset_installs_plan_and_clears_active() -> None:
    s = ClusterScope()
    s.set_active(_cluster("stale"))
    plan = [_cluster("stable"), _cluster("extra_cap")]
    s.reset(plan)
    assert [c.name for c in s.all] == ["stable", "extra_cap"]
    assert s.active is None                       # reset clears the active cluster
    s.all.append(_cluster("x"))                   # reset copies (independent list)
    assert len(plan) == 2


def test_satisfies_augment_cluster_scope_port() -> None:
    from pipeline_core.ports.augment import ClusterScope as ClusterScopePort
    assert isinstance(ClusterScope(), ClusterScopePort)
