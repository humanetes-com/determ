"""Unit tests for the git workspace adapter (pipeline_core/adapters/workspace.py).

Git operations are deterministic, so these run against a REAL throwaway repo built in
tmp_path (no mocking of git) — they exercise the managed/unmanaged gating, the
nothing-to-commit short-circuit, tag/branch effects, and the GitWorkspace wrappers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters import workspace as ws
from pipeline_core.adapters.workspace import GitWorkspace, WorkspaceDeps


class _Recorder:
    """Captures log lines + emit events so assertions can inspect side effects."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.events: list[tuple[str, dict]] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, data: dict | None = None) -> None:
        self.events.append((event, data or {}))

    @property
    def deps(self) -> WorkspaceDeps:
        return WorkspaceDeps(log=self.log, emit=self.emit)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A configured git repo on branch `master` with one initial commit."""
    r = tmp_path / "wsrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=r, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=r, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, capture_output=True, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=r, capture_output=True, check=True)
    (r / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--no-gpg-sign"], cwd=r, capture_output=True, check=True)
    return r


# --------------------------------------------------------------------------- git_commit

def test_git_commit_unmanaged_is_noop(repo: Path) -> None:
    rec = _Recorder()
    (repo / "f.txt").write_text("x")
    assert ws.git_commit(rec.deps, repo, "msg", managed=False) is False
    assert rec.events == []  # never touched git


def test_git_commit_with_changes_commits_and_emits(repo: Path) -> None:
    rec = _Recorder()
    (repo / "f.txt").write_text("new content")
    assert ws.git_commit(rec.deps, repo, "add f", managed=True) is True
    assert _git(repo, "log", "-1", "--format=%s") == "add f"
    assert any("committed: add f" in m for m in rec.logs)
    [(event, data)] = rec.events
    assert event == "git.commit" and data["message"] == "add f" and len(data["workspace_sha"]) == 12


def test_git_commit_no_changes_returns_false(repo: Path) -> None:
    rec = _Recorder()
    # Working tree clean → nothing staged → no commit, no event.
    assert ws.git_commit(rec.deps, repo, "noop", managed=True) is False
    assert rec.events == []


# --------------------------------------------------------------------------- git_tag

def test_git_tag_creates_tag_at_head(repo: Path) -> None:
    rec = _Recorder()
    ws.git_tag(rec.deps, repo, "v1", managed=True)
    assert "v1" in _git(repo, "tag", "--list").splitlines()
    assert any("tagged: v1" in m for m in rec.logs)


def test_git_tag_force_updates_existing(repo: Path) -> None:
    rec = _Recorder()
    ws.git_tag(rec.deps, repo, "v1", managed=True)
    (repo / "f.txt").write_text("y")
    ws.git_commit(rec.deps, repo, "second", managed=True)
    ws.git_tag(rec.deps, repo, "v1", managed=True)  # -f moves it
    head = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "-n", "1", "v1") == head


def test_git_tag_unmanaged_is_noop(repo: Path) -> None:
    rec = _Recorder()
    ws.git_tag(rec.deps, repo, "v1", managed=False)
    assert _git(repo, "tag", "--list") == ""


# --------------------------------------------------------------------------- default_branch / file_hash

def test_git_default_branch(repo: Path) -> None:
    assert ws.git_default_branch(repo) == "master"


def test_git_file_hash_known_and_unknown(repo: Path) -> None:
    assert ws.git_file_hash(repo, "README.md") != "unknown"
    assert ws.git_file_hash(repo, "does_not_exist.rs") == "unknown"


# --------------------------------------------------------------------------- git_clean_reset

def test_git_clean_reset_recreates_pipeline_branch(repo: Path) -> None:
    rec = _Recorder()
    # An uncommitted mess + an untracked file the reset must clean.
    (repo / "README.md").write_text("dirty")
    (repo / "junk.txt").write_text("junk")
    ws.git_clean_reset(rec.deps, repo)
    assert _git(repo, "branch", "--show-current") == "pipeline"
    assert not (repo / "junk.txt").exists()                 # clean -fd removed it
    assert (repo / "README.md").read_text() == "init\n"      # reset --hard restored it
    assert any("recreated pipeline branch" in m for m in rec.logs)


# --------------------------------------------------------------------------- GitWorkspace wrappers

def test_gitworkspace_read_write_roundtrip(repo: Path) -> None:
    gw = GitWorkspace(repo, _Recorder().deps, managed=True)
    gw.write("src/nested/m.rs", "fn main() {}")
    assert gw.read("src/nested/m.rs") == "fn main() {}"   # parent dirs auto-created


def test_gitworkspace_commit_with_tag(repo: Path) -> None:
    rec = _Recorder()
    gw = GitWorkspace(repo, rec.deps, managed=True)
    gw.write("a.txt", "hello")
    gw.commit("ws commit", tag="snap")
    assert _git(repo, "log", "-1", "--format=%s") == "ws commit"
    assert "snap" in _git(repo, "tag", "--list").splitlines()


def test_gitworkspace_default_branch_and_file_hash(repo: Path) -> None:
    gw = GitWorkspace(repo, _Recorder().deps, managed=True)
    assert gw.default_branch() == "master"
    assert gw.file_hash("README.md") != "unknown"


def test_gitworkspace_reset_clean(repo: Path) -> None:
    gw = GitWorkspace(repo, _Recorder().deps, managed=True)
    (repo / "stray.txt").write_text("x")
    gw.reset_clean()
    assert _git(repo, "branch", "--show-current") == "pipeline"
    assert not (repo / "stray.txt").exists()
