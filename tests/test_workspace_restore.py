"""Unit tests for `pipeline_core.adapters.workspace_restore.RestoreFromAugmented` (P6).

Covers the loud-failure guards against a real temp git repo (no subprocess mocking):
missing augmentation tag, and multi-tag → RuntimeError.

Plus the reconstruction archive. `git reset --hard <task0c-augmented-*>` is a
whole-tree rollback over TRACKED directories, and the tag predates every fill —
so on 2026-08-08 the reset deleted 5,137 files, 5,067 of them
`.skeleton-filled`, taking a `pass=95 fail=15 oracle=110` tree off disk. The
tests below build that exact shape (a filled body committed after the tag, then
reset to the tag) and assert the body is reachable afterwards. The negative
control runs the same fixture with the archive disabled and asserts the loss
reproduces — without it these prove nothing.
"""
from __future__ import annotations

import subprocess

import pytest

from pipeline_core.adapters.workspace_restore import RestoreFromAugmented
from pipeline_core.domain.workspace_layout import RECONSTRUCTION_DIRS


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _restore():
    return RestoreFromAugmented(
        read_state=lambda _p: {"phases": {}}, write_state=lambda _p, _s: None,
        log=lambda *_: None,
    )


def _init_repo(repo):
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def test_no_tag_raises(tmp_path):
    repo = tmp_path / "ws"
    _init_repo(repo)
    with pytest.raises(RuntimeError, match="No task0c-augmented"):
        _restore().run(tmp_path, repo)


def test_multi_tag_raises(tmp_path):
    repo = tmp_path / "ws"
    _init_repo(repo)
    _git(repo, "tag", "task0c-augmented-a")
    _git(repo, "tag", "task0c-augmented-b")
    with pytest.raises(RuntimeError, match="Multi-module restore"):
        _restore().run(tmp_path, repo)


# --- the reconstruction archive ------------------------------------------

BODY = "pub fn construct() -> Error { Error::from_boxed(b) }"


def _repo_at_the_2026_08_08_shape(tmp_path):
    """A workspace whose augmentation tag predates the fills, with a real body
    committed after it — the shape run `491052-1af6b4f4` reset over."""
    repo = tmp_path / "ws"
    _init_repo(repo)
    _git(repo, "tag", "task0c-augmented-anyhow")   # tag holds NO reconstruction
    for d in RECONSTRUCTION_DIRS:
        src = repo / d / "stable" / "src"
        src.mkdir(parents=True)
        (src / "error.rs").write_text(BODY, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "task2a checkpoint - stable:95 passing")
    return repo


def _archive_root(tmp_path):
    return tmp_path / "ws.pre_restart"


def test_the_hard_reset_would_delete_the_reconstruction(tmp_path, monkeypatch):
    """NEGATIVE CONTROL. Same fixture, archive suppressed: the reset removes the
    filled bodies. A fix-verification test that does not fail on the pre-fix
    behaviour has proved nothing, so this pins that the hazard is real and that
    the fixture reproduces it."""
    repo = _repo_at_the_2026_08_08_shape(tmp_path)
    monkeypatch.setattr(RestoreFromAugmented, "_archive_reconstruction",
                        lambda _self, _p: None)

    _restore().run(tmp_path, repo)

    for d in RECONSTRUCTION_DIRS:
        assert not (repo / d).exists(), f"{d} survived a reset it cannot survive"
    assert not _archive_root(tmp_path).exists()


def test_the_reconstruction_is_archived_before_the_reset(tmp_path):
    repo = _repo_at_the_2026_08_08_shape(tmp_path)

    _restore().run(tmp_path, repo)

    stamps = list(_archive_root(tmp_path).iterdir())
    assert len(stamps) == 1, f"expected one timestamped archive, got {stamps}"
    for d in RECONSTRUCTION_DIRS:
        kept = stamps[0] / d / "stable" / "src" / "error.rs"
        assert kept.read_text(encoding="utf-8") == BODY, f"{d} not archived intact"


def test_the_layout_and_the_bodies_are_archived_together(tmp_path):
    """The pair rule. t1.5 re-authors the layout every run, so restoring
    `.skeleton-filled/<cluster>` against a different run's `.skeleton/<cluster>`
    yields bodies answering to types the tree no longer has — between `1f4099b`
    and the tree that replaced it, `ErrorImpl` lost
    `pub(crate) backtrace: crate::backtrace::Backtrace`. An archive holding one
    without the other is not a recovery point."""
    repo = _repo_at_the_2026_08_08_shape(tmp_path)

    _restore().run(tmp_path, repo)

    stamp = next(_archive_root(tmp_path).iterdir())
    assert {p.name for p in stamp.iterdir()} == set(RECONSTRUCTION_DIRS)


def test_the_archive_survives_a_second_restart(tmp_path):
    """Never pruned, and never overwritten: two restarts leave two archives.
    Pruning is the class of operation this item exists because of."""
    repo = _repo_at_the_2026_08_08_shape(tmp_path)
    _restore().run(tmp_path, repo)
    first = next(_archive_root(tmp_path).iterdir())

    for d in RECONSTRUCTION_DIRS:                       # a second run fills again
        src = repo / d / "stable" / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "error.rs").write_text("second run", encoding="utf-8")
    _restore().run(tmp_path, repo)

    assert first.joinpath(*RECONSTRUCTION_DIRS[:1], "stable", "src", "error.rs") \
        .read_text(encoding="utf-8") == BODY
    assert len(list(_archive_root(tmp_path).iterdir())) == 2


def test_nothing_to_archive_is_not_an_error(tmp_path):
    """A relaunch with no fills on disk must cost nothing and create nothing."""
    repo = tmp_path / "ws"
    _init_repo(repo)
    _git(repo, "tag", "task0c-augmented-anyhow")

    _restore().run(tmp_path, repo)

    assert not _archive_root(tmp_path).exists()


def test_the_archive_is_outside_the_workspace(tmp_path):
    """`git_commit`'s `git add -A` would sweep an in-workspace archive into the
    next task0c/task1 commit, and `--clean` is `shutil.rmtree` over the
    workspace. Both are why this lands as a sibling of project_dir."""
    repo = _repo_at_the_2026_08_08_shape(tmp_path)

    _restore().run(tmp_path, repo)

    stamp = next(_archive_root(tmp_path).iterdir())
    assert not stamp.is_relative_to(repo)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout == ""
