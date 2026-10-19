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


# --------------------------------------------------------------- build output never tracked
# Run `1680589-7a6824f8` (2026-10-19): the workspace's `.gitignore` is the
# crate's own `/target/`, anchored at the root, so every cluster crate root's
# `target/` was committed by each checkpoint — 26,329 tracked build files.

def _crate_with_nested_build(repo: Path) -> Path:
    (repo / ".gitignore").write_text("/target/\n/Cargo.lock\n")
    fill = repo / ".skeleton-filled" / "stable"
    (fill / "src").mkdir(parents=True)
    (fill / "src" / "lib.rs").write_text("pub fn a() {}\n")
    (fill / "target" / "debug" / "deps").mkdir(parents=True)
    (fill / "target" / "debug" / "deps" / "test_bin").write_bytes(b"\x7fELF" + b"\0" * 64)
    # The sanitizer sweep's sibling build dir (`adapters/asan_sweep`).
    (fill / "target-asan" / "debug").mkdir(parents=True)
    (fill / "target-asan" / "debug" / "asan_bin").write_bytes(b"\x7fELF")
    return fill


def test_premise_root_anchored_ignore_does_not_cover_a_nested_target(repo: Path) -> None:
    # The premise the fix rests on: plain `git add -A` under the crate's own
    # ignore file commits a nested crate root's build output.
    _crate_with_nested_build(repo)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    assert ".skeleton-filled/stable/target/debug/deps/test_bin" in _git(repo, "ls-files", "--cached")


def test_git_commit_never_stages_nested_build_output(repo: Path) -> None:
    rec = _Recorder()
    _crate_with_nested_build(repo)
    assert ws.git_commit(rec.deps, repo, "checkpoint", managed=True) is True
    tracked = _git(repo, "ls-files").splitlines()
    assert ".skeleton-filled/stable/src/lib.rs" in tracked
    assert not [p for p in tracked if "/target" in p]
    # The rule lives in the repo's own exclude file, not in the crate's.
    assert "target/" in (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert (repo / ".gitignore").read_text() == "/target/\n/Cargo.lock\n"


def test_git_commit_untracks_build_output_an_earlier_commit_tracked(repo: Path) -> None:
    rec = _Recorder()
    fill = _crate_with_nested_build(repo)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "legacy", "--no-gpg-sign"], cwd=repo,
                   check=True, capture_output=True)
    (fill / "src" / "lib.rs").write_text("pub fn a() { }\n")
    assert ws.git_commit(rec.deps, repo, "next", managed=True) is True
    assert not [p for p in _git(repo, "ls-files").splitlines() if "/target" in p]
    assert not [p for p in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
                if "/target" in p]
    # Untracked, not deleted: cargo's cache stays on disk.
    assert (fill / "target" / "debug" / "deps" / "test_bin").exists()
    assert (fill / "target-asan" / "debug" / "asan_bin").exists()
    assert ("git.untrack_build_output", {"paths": 2}) in rec.events


def test_git_commit_paths_keeps_out_the_fill_trees_own_target(repo: Path) -> None:
    rec = _Recorder()
    _crate_with_nested_build(repo)
    assert ws.git_commit_paths(rec.deps, repo, [".skeleton-filled/stable"], "cp",
                               managed=True) is True
    tracked = _git(repo, "ls-files").splitlines()
    assert ".skeleton-filled/stable/src/lib.rs" in tracked
    assert not [p for p in tracked if "/target" in p]


def test_exclude_build_output_is_idempotent(repo: Path) -> None:
    rec = _Recorder()
    assert ws.exclude_build_output(rec.deps, repo) == 0
    assert ws.exclude_build_output(rec.deps, repo) == 0
    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count("target/") == 1 and lines.count("target-*/") == 1
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
