"""Characterization for the relocated skeleton-index prompt builders.
The base index is also covered by test_pipeline_smoke; here we pin the cluster
cfg-filtering path of format_skeleton_index_for_cluster."""
from __future__ import annotations

from pipeline_core.domain.models import BuildCluster
from pipeline_core.prompts.skeleton_index import (
    format_skeleton_index,
    format_skeleton_index_for_cluster,
)

_SRC = (
    "pub fn always() { unimplemented!() }\n"
    '#[cfg(feature = "x")]\n'
    "pub fn only_x() { unimplemented!() }\n"
)


def _cluster(active: list[str], excluded: list[str]) -> BuildCluster:
    return BuildCluster(
        name="c", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=excluded, active_cfg=active, prompt_nightly_rule="",
    )


def test_none_cluster_is_unfiltered_base() -> None:
    out = format_skeleton_index_for_cluster([("src/a.rs", _SRC)], None)
    assert out == format_skeleton_index([("src/a.rs", _SRC)])
    assert "pub fn always();" in out and "pub fn only_x();" in out


def test_cluster_elides_false_cfg_item() -> None:
    # feature x excluded → only_x dropped from the index; always kept.
    out = format_skeleton_index_for_cluster([("src/a.rs", _SRC)], _cluster([], ["x"]))
    assert "pub fn always();" in out
    assert "only_x" not in out


def test_cluster_keeps_active_cfg_item() -> None:
    out = format_skeleton_index_for_cluster([("src/a.rs", _SRC)], _cluster(["x"], []))
    assert "pub fn only_x();" in out
