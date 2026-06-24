"""Characterization for the relocated cluster macro-invariants (P6).
Pins variant enumeration + the cluster-active expansion selection."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.macro_invariants import (
    compute_cluster_macro_invariants,
    enumerate_macro_variants,
)
from pipeline_core.domain.models import BuildCluster

_SRC = (
    '#[cfg(feature = "std")]\n'
    "macro_rules! pick { () => { STD_PATH }; }\n"
    '#[cfg(not(feature = "std"))]\n'
    "macro_rules! pick { () => { CORE_PATH }; }\n"
    "macro_rules! solo { () => { ONLY }; }\n"
)


def _cluster(active: list[str], excluded: list[str]) -> BuildCluster:
    return BuildCluster(
        name="c", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=excluded, active_cfg=active, prompt_nightly_rule="",
    )


def test_enumerate_picks_up_cfg_and_names(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    variants = enumerate_macro_variants(tmp_path)
    names = {v[0] for v in variants}
    assert names == {"pick", "solo"}
    # the two `pick` variants carry their cfg preds; solo has none.
    picks = {cfg for name, cfg, _b, _s in variants if name == "pick"}
    assert any("std" in c for c in picks)


def test_none_cluster_is_empty(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    assert compute_cluster_macro_invariants(tmp_path, None) == ""


def test_active_variant_selected_for_cluster(tmp_path: Path) -> None:
    (tmp_path / "m.rs").write_text(_SRC, encoding="utf-8")
    # std excluded → the not(std) variant (CORE_PATH) is the active one.
    out = compute_cluster_macro_invariants(tmp_path, _cluster([], ["std"]))
    assert "pick!(...)" in out and "CORE_PATH" in out
    assert "STD_PATH" not in out
    assert "solo" not in out  # single-variant macro skipped (no ambiguity)
