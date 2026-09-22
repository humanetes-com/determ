"""strip_and_bake_module: guard short-circuits + the bake→commit→tag→
strip→commit-and-mark happy path, all collaborators stubbed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.application import strip_bake
from pipeline_core.application.strip_bake import StripBakeDeps, strip_and_bake_module


def _deps(calls: list[str], *, phase_valid: bool = False, bake_ok: bool = True,
          args: dict[str, tuple[object, ...]] | None = None) -> StripBakeDeps:
    def rec(name: str):
        def f(*_a: object, **_k: object):
            calls.append(name)
            if args is not None:
                args[name] = _a
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
        invalidate_structure=rec("invalidate_structure"),
        preserve_inline_tests=rec("preserve_inline_tests"),
        seal_oracle=rec("seal_oracle"),
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
    args: dict[str, tuple[object, ...]] = {}
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=False, src_hash="abc",
                          deps=_deps(calls, args=args))
    # The module's crate root, not the project root: in a workspace they differ
    # and only the module dir holds the src/ whose tests must be relocated.
    assert args["preserve_inline_tests"] == (tmp_path / "m",)
    # Hard-Rule-11 ordering: relocate inline tests → capture → bake → commit →
    # tag → strip → cache invalidation (strip rewrote mod decls) →
    # commit_and_mark → seal_oracle (the seal is the strip commit: strip is
    # the oracle's last writer). Preservation leads: both the API snapshot and the
    # skeleton must be derived from the file WITHOUT its tests, or the skeleton
    # ships a test body inside the fill routes' writable surface.
    assert calls == ["preserve_inline_tests", "capture", "bake", "git_commit",
                     "git_tag", "strip", "invalidate_structure",
                     "commit_and_mark", "seal_oracle", "set_current"]
    assert args["seal_oracle"] == (tmp_path, "m")


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
    (tmp_path / ".aug_m").write_text("50.0", encoding="utf-8")  # bare-float sentinel (no %)
    deps = StripBakeDeps(**{**deps.__dict__, "tarpaulin_bin": tarp,
                            "log": lambda m: logs.append(m)})
    strip_and_bake_module(tmp_path, "m", tmp_path / "m", crate_type="lib",
                          from_spec=False, src_hash="abc", deps=deps)
    assert any("coverage is 50.0%" in m for m in logs)
    assert calls[:3] == ["preserve_inline_tests", "capture", "bake"]  # proceeded past the warning


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
