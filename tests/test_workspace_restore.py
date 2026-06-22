"""Unit tests for `pipeline_core.adapters.workspace_restore.RestoreFromAugmented` (P6).

Covers the loud-failure guards against a real temp git repo (no subprocess mocking):
missing augmentation tag, and multi-tag → RuntimeError.
"""
from __future__ import annotations

import subprocess

import pytest

from pipeline_core.adapters.workspace_restore import RestoreFromAugmented


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
