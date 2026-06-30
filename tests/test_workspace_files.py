"""Pins the source-file lifecycle adapter (write/delete/lock + manifest/locked
ledgers) at its new home pipeline_core/adapters/workspace_files.py (2026-06-29)."""
from __future__ import annotations

from pipeline_core.adapters import workspace_files as wf
from pipeline_core.adapters.artifacts import load_manifest
from pipeline_core.domain.workspace_layout import LOCKED_MODULES_DIR


def test_write_normalises_paths_and_records_manifest(tmp_path) -> None:
    mp = tmp_path / "manifest.txt"
    n = wf.write_source_files({"crate/foo.rs": "fn a(){}"}, tmp_path, mp, set())
    assert n == 1
    assert (tmp_path / "crate" / "src" / "foo.rs").exists()      # src/ inserted
    assert load_manifest(mp) == ["crate/src/foo.rs"]


def test_write_skips_tests_and_protected(tmp_path) -> None:
    mp = tmp_path / "manifest.txt"
    (tmp_path / "Cargo.toml").write_text("[package]")
    n = wf.write_source_files(
        {"tests/x.rs": "fn t(){}",          # test dir → skip
         "Cargo.toml": "EVIL",              # existing non-source → protect
         "src/lib.rs": "fn a(){}"},          # ok
        tmp_path, mp, set())
    assert n == 1
    assert (tmp_path / "Cargo.toml").read_text() == "[package]"   # untouched
    assert not (tmp_path / "tests" / "x.rs").exists()


def test_write_respects_existing_test_files(tmp_path) -> None:
    mp = tmp_path / "manifest.txt"
    n = wf.write_source_files({"src/known_test.rs": "x"}, tmp_path, mp,
                              {"src/known_test.rs"})
    assert n == 0


def test_delete_removes_manifest_files_and_ghosts(tmp_path) -> None:
    mp = tmp_path / "manifest.txt"
    src = tmp_path / "m" / "src"
    src.mkdir(parents=True)
    (src / "tracked.rs").write_text("fn a(){}")
    wf.save_manifest(mp, ["m/src/tracked.rs"])
    ghost = src / "ghost.rs"
    ghost.write_text("fn g(){}")               # untracked, no test blocks
    keep = src / "with_test.rs"
    keep.write_text("#[test] fn t(){}")        # untracked but has a test → keep
    wf.delete_source_files(tmp_path, mp, module_dir=tmp_path / "m")
    assert not (src / "tracked.rs").exists()
    assert not ghost.exists()
    assert keep.exists()
    assert not mp.exists()


def test_lock_module_backs_up_and_ledgers(tmp_path) -> None:
    mp = tmp_path / "manifest.txt"
    mod = tmp_path / "widget"
    (mod / "src").mkdir(parents=True)
    (mod / "src" / "lib.rs").write_text("fn a(){}")
    wf.save_manifest(mp, ["widget/src/lib.rs"])
    locked = wf.lock_module(tmp_path, "widget", mod, mp)
    assert locked == 1
    assert (tmp_path / LOCKED_MODULES_DIR / "widget" / "widget" / "src" / "lib.rs").exists()
    assert wf.load_locked_modules(tmp_path) == {"widget"}


def test_delete_helper(tmp_path) -> None:
    f = tmp_path / "x"
    f.write_text("hi")
    wf.delete(f)
    assert not f.exists()
    wf.delete(f)  # missing path → no error


def test_write_build_configs(tmp_path) -> None:
    from pipeline_core.domain.models import BuildCluster
    c = BuildCluster(name="stable", env_extra={"RUSTFLAGS": "--cfg x"},
                     tarpaulin_exclude_files=[], excluded_cfg=[],
                     active_cfg=["feat"], prompt_nightly_rule="")
    wf.write_build_configs(tmp_path, [c], {"feat": 12})
    out = (tmp_path / "build_configs.md").read_text()
    assert "## cluster: stable" in out
    assert "env: RUSTFLAGS += --cfg x" in out
    assert "active_cfg: feat" in out
    assert "gated_lines: ~12" in out
