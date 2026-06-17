"""Unit tests for the cargo env builder (P6 engine-core slice) — moved from
pipeline._cargo_env into pipeline_core.adapters.cargo, now scope-injected."""
from __future__ import annotations

from pipeline_core.adapters.cargo import CargoEnv, apply_cluster_env, base_cargo_env
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
