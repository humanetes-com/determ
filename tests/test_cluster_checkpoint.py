"""I5 pins — F2 heal restore tiers are actually POPULATED during phase-c.

The catastrophe (22->14) re-opened because both middle heal tiers were dead:
nothing wrote `.best_tree/<cluster>/` and `.skeleton-filled/` was never
committed, so heal fell straight to the skeleton nuke. These pins assert the
writer produces exactly the layout heal's readers expect.
"""
from __future__ import annotations

import subprocess

from pipeline_core.adapters.workspace import WorkspaceDeps, git_commit_paths
from pipeline_core.application.workspace_lifecycle import save_cluster_best_tree


def _mk_fill_tree(root, cluster, files):
    filled = root / ".skeleton-filled" / cluster
    for rel, body in files.items():
        p = filled / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return filled


def test_best_tree_layout_matches_heal_reader(tmp_path) -> None:
    # save_cluster_best_tree must write exactly where task2_generator's
    # _best_tree_bytes reads: project_dir/.best_tree/<cluster>/<rel>.
    filled = _mk_fill_tree(tmp_path, "stable",
                           {"src/error.rs": "fn a() {}\n",
                            "src/fmt.rs": "fn b() {}\n"})
    n = save_cluster_best_tree(tmp_path, "stable", filled, pass_count=13)
    assert n == 2
    # exact path heal's reader dereferences (heal.py best_tree_bytes(rel)):
    bp = tmp_path / ".best_tree" / "stable" / "src" / "error.rs"
    assert bp.read_text(encoding="utf-8") == "fn a() {}\n"
    assert (tmp_path / ".best_tree" / "stable" / "src" / "fmt.rs").exists()


def test_best_tree_is_cluster_scoped(tmp_path) -> None:
    # Snapshotting one cluster must not wipe another's snapshot.
    f1 = _mk_fill_tree(tmp_path, "stable", {"src/e.rs": "stable\n"})
    f2 = _mk_fill_tree(tmp_path, "nightly", {"src/e.rs": "nightly\n"})
    save_cluster_best_tree(tmp_path, "stable", f1, pass_count=5)
    save_cluster_best_tree(tmp_path, "nightly", f2, pass_count=7)
    save_cluster_best_tree(tmp_path, "stable", f1, pass_count=6)  # re-snap stable
    assert (tmp_path / ".best_tree" / "nightly" / "src" / "e.rs").read_text() \
        == "nightly\n"
    assert (tmp_path / ".best_tree" / "stable" / "src" / "e.rs").read_text() \
        == "stable\n"


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_git_commit_paths_stages_only_fill_tree(tmp_path) -> None:
    # A phase-c checkpoint must commit the fill tree WITHOUT sweeping in a
    # sibling build-artifact dir (the reason we don't reuse `git add -A`).
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    _mk_fill_tree(tmp_path, "stable", {"src/e.rs": "fn a() {}\n"})
    artifact = tmp_path / ".skeleton" / "stable" / "target"
    artifact.mkdir(parents=True)
    (artifact / "big.o").write_text("BUILD ARTIFACT", encoding="utf-8")

    ws = WorkspaceDeps(log=lambda _m: None, emit=lambda *a, **k: None)
    made = git_commit_paths(ws, tmp_path, [".skeleton-filled/stable"],
                            "pipeline: task2a checkpoint — stable:1 passing",
                            managed=True)
    assert made is True
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path,
                             capture_output=True, text=True).stdout
    assert ".skeleton-filled/stable/src/e.rs" in tracked
    assert "target/big.o" not in tracked        # artifact NOT committed


def test_git_commit_paths_noop_when_unmanaged(tmp_path) -> None:
    ws = WorkspaceDeps(log=lambda _m: None, emit=lambda *a, **k: None)
    assert git_commit_paths(ws, tmp_path, ["x"], "m", managed=False) is False
