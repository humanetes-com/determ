"""strip_and_bake_module (P5-S3): guard short-circuits + the bake→commit→tag→
strip→commit-and-mark happy path, all collaborators stubbed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.application import strip_bake
from pipeline_core.application.strip_bake import StripBakeDeps, strip_and_bake_module


def _deps(calls: list[str], *, phase_valid: bool = False, bake_ok: bool = True) -> StripBakeDeps:
    def rec(name: str):
        def f(*_a: object, **_k: object):
            calls.append(name)
            if name == "git_commit":
                return True
            if name == "bake":
                return {"ok": bake_ok, "clusters": ["default"], "reason": "x"}
            if name == "capture":
                return None
            return None
        return f

    return StripBakeDeps(
        log=lambda _m: None,
        phase_valid=lambda _pd, _k: phase_valid,
        module_augmented_path=lambda pd, m: pd / f".aug_{m}",
        capture_api_surface=rec("capture"),
        bake_skeleton_artifacts=rec("bake"),
        git_commit=rec("git_commit"),
        git_tag=rec("git_tag"),
        strip_module_source=rec("strip"),
        commit_and_mark=rec("commit_and_mark"),
        set_current=rec("set_current"),
        tarpaulin_bin=Path("/nonexistent/tarpaulin"),
        coverage_target=98.0,
    )


def test_from_spec_is_noop(tmp_path: Path) -> None:
    calls: list[str] = []
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=True, src_hash=None, deps=_deps(calls))
    assert calls == []


def test_already_stripped_is_noop(tmp_path: Path) -> None:
    calls: list[str] = []
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=False, src_hash=None,
                          deps=_deps(calls, phase_valid=True))
    assert calls == []


def test_happy_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `git status --porcelain` → dirty tree so the augmentation commit fires.
    monkeypatch.setattr(
        strip_bake.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, " M x.rs\n", ""))
    calls: list[str] = []
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=False, src_hash="abc", deps=_deps(calls))
    # Hard-Rule-11 ordering: capture → bake → commit → tag → strip → commit_and_mark.
    assert calls == ["capture", "bake", "git_commit", "git_tag", "strip",
                     "commit_and_mark", "set_current"]


def test_low_coverage_warns_then_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strip_bake.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, " M x\n", ""))
    logs: list[str] = []
    calls: list[str] = []
    deps = _deps(calls)
    # tarpaulin "exists" + sentinel below target → low-coverage WARNING branch.
    tarp = tmp_path / "tarpaulin"
    tarp.write_text("", encoding="utf-8")
    (tmp_path / ".aug_m").write_text("50%", encoding="utf-8")
    deps = StripBakeDeps(**{**deps.__dict__, "tarpaulin_bin": tarp,
                            "log": lambda m: logs.append(m)})
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=False, src_hash="abc", deps=deps)
    assert any("coverage is 50.0%" in m for m in logs)
    assert calls[:2] == ["capture", "bake"]  # proceeded past the warning


def test_bake_failure_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strip_bake.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    calls: list[str] = []
    with pytest.raises(RuntimeError, match="skeleton bake failed"):
        strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                              from_spec=False, src_hash=None,
                              deps=_deps(calls, bake_ok=False))
    assert "strip" not in calls  # never strips when bake fails
