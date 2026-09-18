"""Unit tests for `pipeline_core.adapters.workspace_restore.RestoreFromAugmented`.

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


# --- the PRE-augmentation restore point -----------------------------------
# `task0c-augmented-*` is the point AFTER task0c: a restore to it brings the
# generated tests back. A run whose generator or admission changed needs the
# commit task0c built on. The fixture below is that shape on a temp repo:
# commit A = the `pipeline: task0b done` state (tag task0c-pre-x), commit B =
# what task0c + strip + a fill added on top.

from pipeline_core.adapters.state import read_state, write_state  # noqa: E402
from pipeline_core.adapters.workspace_restore import RestoreFromPreAugment  # noqa: E402

_STATE_A = """# pipeline_state — ws
# 2026-08-26 00:00:00

[cfg_scan]  done          hash=a4b46572a4794e73  2 clusters: stable, nightly
[test_cmd]  done          hash=b6fa9024e0dacaf3  cargo test
[task0a-context]  done          hash=-  3052 chars
[task0a]  done          hash=-  1466 chars
[task0b]  done          hash=-  3268 chars

current: task0b
"""
_STATE_B = _STATE_A.replace("\ncurrent: task0b\n", """[task0c/stable]  done          hash=a4b46572a4794e73  1 modules
[task0c/stable/x]  done          hash=b6fa9024e0dacaf3  97.8%
[strip/x]  done          hash=5094a2ef48b980d1  
[task1/x]  done          hash=a4b46572a4794e73  27862 chars

current: task2a/x / cluster stable (1/2)
""")


def _pre_augment_restore(log=None):
    return RestoreFromPreAugment(read_state=read_state, write_state=write_state,
                                 log=log or (lambda *_: None))


def _repo_with_a_pre_augment_point(tmp_path):
    repo = tmp_path / "ws"
    _init_repo(repo)
    (repo / ".gitignore").write_text("/target/\n/Cargo.lock\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("pub fn f() {}", encoding="utf-8")
    (repo / "coverage_strategy.md").write_text("strategy", encoding="utf-8")
    (repo / "pipeline_state.md").write_text(_STATE_A, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pipeline: task0b done")
    _git(repo, "tag", "task0c-pre-x")
    # commit B: what task0c, strip and the loop added on top of A.
    (repo / "src" / "x.gen_tests_1.rs").write_text("#[test] fn t() {}", encoding="utf-8")
    for d in RECONSTRUCTION_DIRS:
        src = repo / d / "stable" / "src"
        src.mkdir(parents=True)
        (src / "error.rs").write_text(BODY, encoding="utf-8")
    for sentinel in (repo / ".cluster_augmented" / "stable__x", repo / ".module_augmented" / "x"):
        sentinel.parent.mkdir()
        sentinel.write_text("97.8\n", encoding="utf-8")
    (repo / ".task0c_attempts.jsonl").write_text('{"attempt": 1}\n', encoding="utf-8")
    (repo / "functional_requirements_x.md").write_text("FR", encoding="utf-8")
    (repo / ".module_stripped").mkdir()
    (repo / "pipeline_state.md").write_text(_STATE_B, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pipeline: task2a checkpoint - stable:95 passing")
    return repo


def test_pre_augment_restore_forgets_everything_task0c_and_later_wrote(tmp_path):
    repo = _repo_with_a_pre_augment_point(tmp_path)

    _pre_augment_restore().run(tmp_path, repo)

    assert not (repo / "src" / "x.gen_tests_1.rs").exists()
    assert not (repo / ".cluster_augmented").exists()
    assert not (repo / ".module_augmented").exists()
    assert not (repo / ".task0c_attempts.jsonl").exists()
    assert not (repo / "functional_requirements_x.md").exists()
    assert not (repo / ".module_stripped").exists()
    for d in RECONSTRUCTION_DIRS:
        assert not (repo / d).exists()
    # task0a/0b outputs and the original source survive.
    assert (repo / "src" / "lib.rs").read_text(encoding="utf-8") == "pub fn f() {}"
    assert (repo / "coverage_strategy.md").exists()


def test_pre_augment_restore_archives_the_reconstruction_rather_than_deleting_it(tmp_path):
    repo = _repo_with_a_pre_augment_point(tmp_path)

    _pre_augment_restore().run(tmp_path, repo)

    stamps = list(_archive_root(tmp_path).iterdir())
    assert len(stamps) == 1
    for d in RECONSTRUCTION_DIRS:
        assert (stamps[0] / d / "stable" / "src" / "error.rs").read_text(encoding="utf-8") == BODY


def test_pre_augment_restore_state_keeps_task0b_and_drops_task0c_and_strip(tmp_path):
    repo = _repo_with_a_pre_augment_point(tmp_path)

    _pre_augment_restore().run(tmp_path, repo)

    state = read_state(repo)
    assert set(state["phases"]) == {"cfg_scan", "test_cmd", "task0a-context", "task0a", "task0b"}
    assert state["current"] == "restart-from-pre-augment"


def test_pre_augment_restore_cleans_untracked_but_not_ignored(tmp_path):
    """`git clean -fd`, not `-fdx`: an untracked leftover goes, cargo's
    ignored `target/` (hundreds of MB, minutes to rebuild) stays."""
    repo = _repo_with_a_pre_augment_point(tmp_path)
    (repo / ".phase_c_logs").mkdir()
    (repo / ".phase_c_logs" / "retrospectives.json").write_text("{}", encoding="utf-8")
    (repo / "target" / "debug").mkdir(parents=True)
    (repo / "target" / "debug" / "libx.rlib").write_text("bin", encoding="utf-8")
    lines: list[str] = []

    _pre_augment_restore(lines.append).run(tmp_path, repo)

    assert not (repo / ".phase_c_logs").exists()
    assert (repo / "target" / "debug" / "libx.rlib").exists()
    # git clean names the untracked DIRECTORY it removed, not each file in it.
    assert any(ln.strip() == "Deleted .phase_c_logs/ (untracked)" for ln in lines), lines


def test_pre_augment_restore_describes_what_the_reset_deletes(tmp_path):
    """The reset removes TRACKED files silently; the record must say which."""
    repo = _repo_with_a_pre_augment_point(tmp_path)
    lines: list[str] = []

    _pre_augment_restore(lines.append).run(tmp_path, repo)

    described = [ln for ln in lines if "the reset to task0c-pre-x deletes" in ln]
    assert len(described) == 1, lines
    assert "src/ (1)" in described[0] and ".task0c_attempts.jsonl" in described[0]
    # the reconstruction was archived BEFORE the description, so it is not in it.
    assert ".skeleton-filled" not in described[0]


def test_pre_augment_restore_no_tag_raises_with_the_by_hand_command(tmp_path):
    repo = tmp_path / "ws"
    _init_repo(repo)
    _git(repo, "tag", "task0c-augmented-x")       # the OTHER restore point is not enough
    with pytest.raises(RuntimeError, match="No task0c-pre-.*git -C <workspace> tag task0c-pre-<module> <sha>"):
        _pre_augment_restore().run(tmp_path, repo)


def test_pre_augment_restore_accepts_several_tags_on_one_commit_only(tmp_path):
    repo = tmp_path / "ws"
    _init_repo(repo)
    _git(repo, "tag", "task0c-pre-a")
    _git(repo, "tag", "task0c-pre-b")
    (repo / "g.txt").write_text("y", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    _pre_augment_restore().run(tmp_path, repo)     # same commit: fine
    assert not (repo / "g.txt").exists()

    (repo / "h.txt").write_text("z", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "even later")
    _git(repo, "tag", "-f", "task0c-pre-b", "HEAD")   # b now names a different commit
    with pytest.raises(RuntimeError, match="different commits"):
        _pre_augment_restore().run(tmp_path, repo)


def test_the_augmented_restore_on_the_same_repo_keeps_the_generated_tests(tmp_path):
    """NEGATIVE CONTROL for the gap this restore closes: on the same repo,
    tagged task0c-augmented-x at commit B, the existing restore brings the
    generated test back and leaves the augmentation sentinels standing — which
    is exactly why a changed generator could not be re-run through it."""
    repo = _repo_with_a_pre_augment_point(tmp_path)
    _git(repo, "tag", "task0c-augmented-x")

    _restore().run(tmp_path, repo)

    assert (repo / "src" / "x.gen_tests_1.rs").exists()
    assert (repo / ".cluster_augmented" / "stable__x").exists()
    assert (repo / ".task0c_attempts.jsonl").exists()
    assert not (repo / "functional_requirements_x.md").exists()   # strip/task1+ still wiped
    assert not (repo / ".module_stripped").exists()
