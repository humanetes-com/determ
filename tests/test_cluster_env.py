"""A cluster's cfg holds for rustdoc as it holds for rustc, at every site that
builds a cluster cargo env.

Run `292191-d60a5308` (2026-10-02): the nightly cluster delivered its cfg as
`RUSTFLAGS=--cfg N`, which cargo hands to rustc only, so every doc test ran in
the `not(N)` world and a doc example its crate gates on `N` passed as an empty
shell — in the reconstruction and in the reference measurement alike.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.application import reference_run
from pipeline_core.application.reference_run import (
    ReferenceDeps,
    _measure_cluster,
    reference_key,
)
from pipeline_core.domain.cluster_env import (
    WORLD_VAR,
    cfg_flags,
    overlay_cluster_env,
    world_env_extra,
    world_key,
    world_of_env,
)
from pipeline_core.domain.models import BuildCluster


def _cluster(env_extra: dict[str, str], *, nightly: bool = False) -> BuildCluster:
    return BuildCluster(name="alt", env_extra=env_extra, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                        is_default=False, requires_nightly=nightly)


def test_cfg_flags_reads_both_spellings_in_order() -> None:
    assert cfg_flags('-A warnings --cfg a --cfg=b -C opt-level=1 --cfg feature="c"') == [
        "a", "b", 'feature="c"']
    assert cfg_flags("") == []


def test_every_rustc_cfg_is_delivered_to_rustdoc() -> None:
    assert world_env_extra({"RUSTFLAGS": "--cfg a --cfg=b"}) == {
        "RUSTFLAGS": "--cfg a --cfg=b", "RUSTDOCFLAGS": "--cfg a --cfg b"}


def test_a_cfg_rustdoc_already_has_is_not_repeated_and_the_rule_is_idempotent() -> None:
    once = world_env_extra({"RUSTFLAGS": "--cfg a --cfg b",
                            "RUSTDOCFLAGS": "--cfg b -D warnings"})
    assert once["RUSTDOCFLAGS"] == "--cfg b -D warnings --cfg a"
    assert world_env_extra(once) == once


def test_a_cluster_with_no_cfg_contributes_no_rustdoc_flags() -> None:
    assert world_env_extra({}) == {}
    assert world_env_extra(None) == {}
    assert world_env_extra({"RUSTFLAGS": "-C debuginfo=0", "FOO": "1"}) == {
        "RUSTFLAGS": "-C debuginfo=0", "FOO": "1"}


def test_overlay_appends_both_flag_variables_and_sets_the_rest() -> None:
    env = {"RUSTFLAGS": "-A warnings", "RUSTDOCFLAGS": "-A warnings"}
    overlay_cluster_env(env, _cluster({"RUSTFLAGS": "--cfg a", "FOO": "1"},
                                      nightly=True))
    assert env == {"RUSTFLAGS": "-A warnings --cfg a",
                   "RUSTDOCFLAGS": "-A warnings --cfg a",
                   "FOO": "1", "RUSTUP_TOOLCHAIN": "nightly",
                   WORLD_VAR: "alt"}
    assert world_of_env(env) == "alt" and world_of_env({}) == "" and world_of_env(None) == ""


def test_overlay_of_no_cluster_changes_nothing() -> None:
    env = {"RUSTFLAGS": "-A warnings"}
    overlay_cluster_env(env, None)
    assert env == {"RUSTFLAGS": "-A warnings"}


def test_the_cluster_cargo_env_hands_rustdoc_the_cluster_cfg() -> None:
    """The plan's sentence: under a cluster that sets a cfg, rustdoc gets it.
    NEGATIVE CONTROL: the cluster's stored `env_extra`, which is what reached
    the env before, names no RUSTDOCFLAGS at all."""
    cluster = _cluster({"RUSTFLAGS": "--cfg a"})
    assert "RUSTDOCFLAGS" not in cluster.env_extra
    env = cluster_cargo_env(cluster)
    assert "a" in cfg_flags(env["RUSTDOCFLAGS"])
    assert "a" in cfg_flags(env["RUSTFLAGS"])
    assert "RUSTDOCFLAGS" not in cluster.env_extra   # the baked plan is not edited


def test_the_reference_is_measured_under_the_overlay_when_no_env_is_bound(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, dict[str, str]] = {}

    def _run(_cmd, **kw):  # noqa: ANN001, ANN003
        seen["env"] = dict(kw["env"])
        return subprocess.CompletedProcess(_cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(reference_run.subprocess, "run", _run)
    deps = ReferenceDeps(cargo_env=lambda: {"RUSTFLAGS": "-A warnings"},
                         parse_cmd=str.split, enumerate_fns=lambda _p: [])
    _measure_cluster(tmp_path, SimpleNamespace(
        name="alt", toolchain=None, env_extra={"RUSTFLAGS": "--cfg a"}), deps)
    assert seen["env"]["RUSTFLAGS"] == "-A warnings --cfg a"
    assert seen["env"]["RUSTDOCFLAGS"] == "--cfg a"


def test_the_reference_key_follows_the_env_cargo_runs_under(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A measurement taken while the cfg reached rustc only must not be reused
    once it reaches rustdoc, although `env_extra` is byte-identical: the key
    hashes the effective env. Two spellings of one world share a key."""
    monkeypatch.setattr(reference_run, "_toolchain_id", lambda _t: "rustc x")
    rustc_only = SimpleNamespace(name="alt", toolchain=None,
                                 env_extra={"RUSTFLAGS": "--cfg a"})
    both = SimpleNamespace(name="alt", toolchain=None,
                           env_extra={"RUSTFLAGS": "--cfg a",
                                      "RUSTDOCFLAGS": "--cfg a"})
    other = SimpleNamespace(name="alt", toolchain=None,
                            env_extra={"RUSTFLAGS": "--cfg b"})
    key = reference_key([rustc_only], "master", "t")
    assert key == reference_key([both], "master", "t")
    assert key != reference_key([other], "master", "t")


def test_task3_runs_each_cluster_under_the_overlay_when_no_env_is_bound(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third hand-built site. With `cluster_env` unbound, task3's cluster
    run builds its env from `cargo_env()` through the one rule: the base flags
    survive and rustdoc gets the cfg. A `dict.update(env_extra)` there prints
    `RUSTFLAGS=--cfg a RUSTDOCFLAGS=` and fails this."""
    from contextlib import contextmanager

    import pipeline_core.log as pclog
    from pipeline_core.application.tasks.task3 import Task3Deps, run_clusters_strict

    monkeypatch.setattr(pclog, "_log_sink", lambda m: None)
    monkeypatch.setattr(pclog, "_emit_sink", lambda e, **f: None)

    @contextmanager
    def _phase(_kind: str, **_kw: object):  # noqa: ANN202
        yield {}

    deps = Task3Deps(
        phase=_phase,
        load_baked_clusters=lambda _p: [],
        task3_legacy_single=lambda *a, **k: (False, ""),
        cargo_env=lambda: {"PATH": "/usr/bin:/bin", "RUSTFLAGS": "-A warnings"},
        parse_cmd=lambda _cmd: [
            "sh", "-c",
            'echo "seen RUSTFLAGS=$RUSTFLAGS RUSTDOCFLAGS=$RUSTDOCFLAGS"; '
            'echo "test result: ok. 1 passed; 0 failed; 0 ignored"'],
        count_active_tests_for_cluster=lambda _p, _c: 1,
        store_prompt_content=lambda _s: "",
        task3_inline_retry=lambda **_k: (False, ""),
        read=lambda _p: "",
        write=lambda _p, _s: None,
        test_timeout=30,
    )
    cluster = SimpleNamespace(name="alt", toolchain="",
                              env_extra={"RUSTFLAGS": "--cfg a"})
    _ok, out, _results = run_clusters_strict(tmp_path, [cluster], deps)
    assert "seen RUSTFLAGS=-A warnings --cfg a RUSTDOCFLAGS=--cfg a" in out, out


def test_world_key_separates_clusters_only_by_what_changes_their_cargo_run() -> None:
    """Two spellings of one world share a key; a different cfg or toolchain
    does not; and a cluster that contributes no flags (a cargo-feature
    cluster) has the DEFAULT cluster's key, whatever it is called — keyed on
    the name it would be counted as a world of its own."""
    default = _cluster({})
    feature = BuildCluster(name="feature/x", env_extra={}, tarpaulin_exclude_files=[],
                           excluded_cfg=[], active_cfg=["x"], prompt_nightly_rule="",
                           is_default=False)
    assert world_key(feature) == world_key(default) == world_key(None)
    assert world_key(_cluster({"RUSTFLAGS": "--cfg a"})) == world_key(
        _cluster({"RUSTFLAGS": "--cfg a", "RUSTDOCFLAGS": "--cfg a"}))
    assert world_key(_cluster({"RUSTFLAGS": "--cfg a"})) != world_key(default)
    assert world_key(_cluster({}, nightly=True)) != world_key(default)


def test_the_composition_binds_the_measured_world_key() -> None:
    """Dropping the binding would fail at construction (`world_key` has no
    default); binding something else is caught here."""
    import inspect

    from pipeline_core import composition
    src = inspect.getsource(composition)
    assert "world_key=world_key" in src
    assert composition.world_key is world_key
