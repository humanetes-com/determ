"""The t1.5 layout gate's compile check must build under the CLUSTER's own
toolchain, not merely its env vars.

Regression: the gate took a loose ``env_extra`` mapping. anyhow's nightly
cluster carries ``RUSTFLAGS=--cfg error_generic_member_access`` *and*
``requires_nightly``; a mapping carries only the former, so the gate switched
the nightly-only blocks on and compiled them with the default toolchain —
E0554 at the crate root, so all 14 components failed all 3 attempts while the
identical components authored 12/14 on stable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pipeline_core.adapters.skeleton_compile as sc
from pipeline_core.domain.models import BuildCluster


def _cluster(name: str, *, env_extra=None, nightly=False) -> BuildCluster:
    return BuildCluster(name=name, env_extra=env_extra or {}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                        requires_nightly=nightly)


def _skeleton(tmp_path: Path, cluster: str) -> Path:
    src = tmp_path / ".skeleton" / cluster / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text("pub fn f() { unimplemented!() }\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    return tmp_path


class _Recorder:
    """Stands in for subprocess.run, capturing the env the gate would use."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode, self.stderr = returncode, stderr
        self.env: dict[str, str] = {}

    def __call__(self, argv, **kw):  # noqa: ANN001, ANN204
        self.env = dict(kw.get("env") or {})
        return type("P", (), {"returncode": self.returncode,
                              "stderr": self.stderr, "stdout": ""})()


def test_nightly_cluster_selects_the_nightly_toolchain(tmp_path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    pd = _skeleton(tmp_path, "nightly")
    ok, _ = sc.compile_skeleton_check(
        pd, "nightly",
        cluster=_cluster("nightly", env_extra={"RUSTFLAGS": "--cfg egma"}, nightly=True))
    assert ok
    # The half a mapping cannot carry — without it the cfg below builds on stable.
    assert rec.env["RUSTUP_TOOLCHAIN"] == "nightly"
    # ...and the cfg is APPENDED to the base flags, never clobbering them.
    assert rec.env["RUSTFLAGS"] == "-A warnings --cfg egma"


def test_stable_cluster_pins_no_toolchain(tmp_path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    pd = _skeleton(tmp_path, "stable")
    sc.compile_skeleton_check(pd, "stable", cluster=_cluster("stable"))
    assert "RUSTUP_TOOLCHAIN" not in rec.env


def test_absent_cluster_fails_conservatively_and_loudly(tmp_path, monkeypatch) -> None:
    """A cluster that could not be resolved must never fall back to a guessed
    toolchain — that silent fallback is the whole bug class. False leaves the
    component frozen (safe); the reason names the cluster."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    pd = _skeleton(tmp_path, "nightly")
    ok, reason = sc.compile_skeleton_check(pd, "nightly", cluster=None)
    assert not ok
    assert "nightly" in reason and "toolchain" in reason
    assert rec.env == {}          # never reached cargo


def test_error_excerpt_is_bounded_and_head_kept(tmp_path, monkeypatch) -> None:
    stderr = "error[E0554]: `#![feature]` may not be used\n" + ("x" * 20000)
    monkeypatch.setattr(subprocess, "run", _Recorder(returncode=1, stderr=stderr))
    pd = _skeleton(tmp_path, "nightly")
    ok, excerpt = sc.compile_skeleton_check(
        pd, "nightly", cluster=_cluster("nightly", nightly=True))
    assert not ok
    assert excerpt.startswith("error[E0554]")
    assert len(excerpt) <= 5000
