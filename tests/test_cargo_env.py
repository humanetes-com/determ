"""Unit tests for the cargo env builder (P6 engine-core slice) — moved from
pipeline._cargo_env into pipeline_core.adapters.cargo, now scope-injected."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import (
    CargoEnv,
    apply_cluster_env,
    base_cargo_env,
    cluster_cargo_env,
)
from pipeline_core.adapters.structure import load_baked_clusters
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.models import BuildCluster


def _cluster(name: str, *, env_extra=None, nightly=False) -> BuildCluster:
    return BuildCluster(name=name, env_extra=env_extra or {}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                        requires_nightly=nightly)


def test_base_env_static() -> None:
    e = base_cargo_env()
    assert e["RUSTFLAGS"] == "-A warnings"
    assert e["CARGO_TERM_COLOR"] == "never"
    assert ".cargo/bin" in e["PATH"]
    assert "RUST_BACKTRACE" not in e          # deliberately unset (coverage parity)


def test_apply_cluster_env_appends_rustflags_and_sets_nightly() -> None:
    e = {"RUSTFLAGS": "-A warnings"}
    apply_cluster_env(e, _cluster("x", env_extra={"RUSTFLAGS": "--cfg extra_cap"}, nightly=True))
    assert e["RUSTFLAGS"] == "-A warnings --cfg extra_cap"   # appended, base survives
    assert e["RUSTUP_TOOLCHAIN"] == "nightly"


def test_apply_cluster_env_sets_non_rustflags_keys() -> None:
    e = {"RUSTFLAGS": "-A warnings"}
    apply_cluster_env(e, _cluster("x", env_extra={"FOO": "bar"}))
    assert e["FOO"] == "bar" and e["RUSTFLAGS"] == "-A warnings"


def test_apply_cluster_env_none_is_noop() -> None:
    e = {"RUSTFLAGS": "-A warnings"}
    apply_cluster_env(e, None)
    assert e == {"RUSTFLAGS": "-A warnings"}


def test_cargoenv_callable_reads_active_at_call_time() -> None:
    scope = ClusterScope()
    cargo_env = CargoEnv(scope)
    assert "RUSTUP_TOOLCHAIN" not in cargo_env()        # no active cluster
    scope.set_active(_cluster("nightly", env_extra={"RUSTFLAGS": "--cfg q"}, nightly=True))
    e = cargo_env()
    assert e["RUSTFLAGS"] == "-A warnings --cfg q" and e["RUSTUP_TOOLCHAIN"] == "nightly"
    scope.set_active(None)
    assert "RUSTUP_TOOLCHAIN" not in cargo_env()


def test_cluster_cargo_env_is_scope_independent_and_carries_the_toolchain() -> None:
    """The env for a named cluster, whatever the active scope: cfg appended
    to `-A warnings`, RUSTUP_TOOLCHAIN set when the cluster requires nightly."""
    e = cluster_cargo_env(_cluster("nightly", env_extra={"RUSTFLAGS": "--cfg q"}, nightly=True))
    assert e["RUSTFLAGS"] == "-A warnings --cfg q"
    assert e["RUSTUP_TOOLCHAIN"] == "nightly"
    assert ".cargo/bin" in e["PATH"]
    assert "RUSTUP_TOOLCHAIN" not in cluster_cargo_env(_cluster("stable"))
    assert "RUSTUP_TOOLCHAIN" not in cluster_cargo_env(None)


def test_cluster_cargo_env_on_the_recorded_nightly_plan() -> None:
    """Fixture: the baked cluster plan on disk (`.skeleton/_clusters.json`),
    whose nightly cluster is env_extra RUSTFLAGS=--cfg
    error_generic_member_access + requires_nightly. Negative control: the
    old task2 composition (active-scope env, never set in task2, then
    dict.update with env_extra) — the rustflags the assert probe's stage
    recorded under the stable rustc on run 216855-03b2a21b."""
    project = Path(__file__).resolve().parent.parent / "tmp" / "anyhow"
    clusters = {c.name: c for c in load_baked_clusters(project)}
    if "nightly" not in clusters:
        pytest.skip("no baked anyhow cluster plan on this checkout")
    nightly = clusters["nightly"]
    assert nightly.requires_nightly and "--cfg" in nightly.env_extra.get("RUSTFLAGS", "")
    good = cluster_cargo_env(nightly)
    assert good["RUSTUP_TOOLCHAIN"] == "nightly"
    assert good["RUSTFLAGS"].split() == ["-A", "warnings", *nightly.env_extra["RUSTFLAGS"].split()]
    old = CargoEnv(ClusterScope())()
    old.update(nightly.env_extra)
    assert old["RUSTFLAGS"].split() == nightly.env_extra["RUSTFLAGS"].split()
    assert "RUSTUP_TOOLCHAIN" not in old
