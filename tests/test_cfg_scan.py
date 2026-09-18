"""Cluster identification — `task0_cfg_scan`.

A custom `--cfg` toggle that gates ANY code must form its own compiler cluster: if it
doesn't, the gated code is reconstructed/tested in NO cluster (a silent coverage hole +
the strict-count confusion that masked the cap trap). Declared cargo features keep a noise
threshold so a feature-heavy crate doesn't explode into clusters.
"""
from __future__ import annotations

from pathlib import Path

import pipeline


def _scan(tmp_path: Path, lib_rs: str, cargo_toml: str = '[package]\nname="x"\nversion="0.1.0"\n'):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "Cargo.toml").write_text(cargo_toml)
    (tmp_path / "src" / "lib.rs").write_text(lib_rs)
    return pipeline.task0_cfg_scan(tmp_path)


def _names(clusters) -> list[str]:
    return [c.name for c in clusters]


def test_lone_custom_cfg_forms_a_cluster(tmp_path: Path) -> None:
    """A single `#[cfg(extra_cap)]` line (no `not()` counterpart) must still create
    the extra_cap-active cluster — the hole that dropped the cap trap."""
    clusters = _scan(tmp_path,
                     "#[cfg(extra_cap)]\npub fn big() -> usize { 999 }\n"
                     "pub fn small() -> usize { 1 }\n")
    assert "stable" in _names(clusters)
    assert "extra_cap" in _names(clusters)
    extra = next(c for c in clusters if c.name == "extra_cap")
    assert extra.active_cfg == ["extra_cap"] and not extra.is_default
    stable = next(c for c in clusters if c.name == "stable")
    assert "extra_cap" in stable.excluded_cfg


def test_dual_form_custom_cfg_is_one_cluster(tmp_path: Path) -> None:
    """`#[cfg(x)]` + `#[cfg(not(x))]` flatten to the same atom → one extra cluster,
    not two."""
    clusters = _scan(tmp_path,
                     "#[cfg(extra_cap)]\npub fn b() -> usize { 9 }\n"
                     "#[cfg(not(extra_cap))]\npub fn b() -> usize { 1 }\n")
    assert sorted(_names(clusters)) == ["extra_cap", "stable"]


def test_declared_feature_below_threshold_is_noise(tmp_path: Path) -> None:
    """A DECLARED cargo feature gating few lines stays out (noise threshold) so a
    feature-heavy crate doesn't explode into clusters."""
    clusters = _scan(tmp_path,
                     '#[cfg(feature="rare")]\npub fn r() {}\npub fn s() {}\n',
                     cargo_toml='[package]\nname="x"\nversion="0.1.0"\n[features]\nrare=[]\n')
    assert _names(clusters) == ["stable"]


def test_gen_tests_cfg_never_clusters(tmp_path: Path) -> None:
    """A custom cfg that appears ONLY in an LLM-authored `*.gen_tests_<N>.rs`
    sibling must NOT form a cluster (Hard Rule 8: cluster identity is static, from
    real source only). Regression: a stray `#[cfg(not(anyhow_no_core_error))]` in an
    augmented test spawned a phantom cluster whose harness never compiled → 0%
    coverage → blind reconstruction. The real-source cfg beside it still clusters."""
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\nversion="0.1.0"\n')
    (tmp_path / "src" / "lib.rs").write_text(
        "#[cfg(real_cap)]\npub fn big() -> usize { 9 }\npub fn small() -> usize { 1 }\n")
    (tmp_path / "src" / "wrapper.gen_tests_4.rs").write_text(
        "#[cfg(phantom_cap)]\npub fn ghost() -> usize { 0 }\n")
    clusters = pipeline.task0_cfg_scan(tmp_path)
    assert "real_cap" in _names(clusters)
    assert "phantom_cap" not in _names(clusters)


def test_builtin_cfg_never_clusters(tmp_path: Path) -> None:
    """Platform/builtin cfgs (`unix`, `test`, `target_os`) never form a cluster
    regardless of how many lines they gate."""
    body = "\n".join(f'#[cfg(unix)]\npub fn u{i}() {{}}' for i in range(8))
    clusters = _scan(tmp_path, body + "\n")
    assert _names(clusters) == ["stable"]
