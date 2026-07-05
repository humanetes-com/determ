"""Pins for the TDD-iter batch ratchet helpers (debt #45)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.tdd_ratchet import (
    restore_src_tree,
    snapshot_src_tree,
)


def _mk_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text("orig-a", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.rs").write_text("orig-b", encoding="utf-8")
    return src


def test_snapshot_captures_all_rs_recursively(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    assert {Path(p).name for p in snap} == {"a.rs", "b.rs"}


def test_snapshot_none_when_root_absent(tmp_path: Path) -> None:
    assert snapshot_src_tree(tmp_path / "nope") is None


def test_restore_only_rewrites_changed_files(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    # a.rs regresses (a bad fill), b.rs unchanged.
    (src / "a.rs").write_text("BAD-FILL", encoding="utf-8")
    reverted = restore_src_tree(snap)
    assert reverted == 1                                   # only a.rs rewritten
    assert (src / "a.rs").read_text(encoding="utf-8") == "orig-a"
    assert (src / "sub" / "b.rs").read_text(encoding="utf-8") == "orig-b"


def test_restore_noop_when_nothing_changed(tmp_path: Path) -> None:
    src = _mk_src(tmp_path)
    snap = snapshot_src_tree(src)
    assert snap is not None
    assert restore_src_tree(snap) == 0

    # A file created AFTER the snapshot is left alone (never in the snapshot).
    (src / "c.rs").write_text("new", encoding="utf-8")
    assert restore_src_tree(snap) == 0
    assert (src / "c.rs").exists()
