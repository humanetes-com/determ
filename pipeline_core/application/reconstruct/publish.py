"""Publish the one tree task 2a judged into the workspace crate, and check
that the workspace still holds it.

Task 2b used to MERGE one tree per cluster into `src/`, and three writers
then edited `src/` after it (the post-merge fill, task 3's inline retry, a
whole-file fix loop). None of their bodies were in the cluster trees, so the
next re-entry's merge overwrote them: on the anyhow workspace after the
2026-10-19 task 3 restarts, `src/` differed from `.skeleton-filled/stable/src`
by 108 lines in 4 files, `provide` filled in one and `unimplemented!()` in the
other. With one tree there is nothing to merge, and publishing is a byte
copy. The drift check is what makes it the only writer: task 3 refuses to
judge a `src/` that is not the tree the loop judged, so a later writer of
`src/` fails the next task 3 instead of being overwritten in silence.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.tree_txn import TreeTxn


def _sources(root: Path) -> dict[str, bytes]:
    src = root / "src"
    if not src.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(src.rglob("*.rs")) if p.is_file()}


def publish_tree(tree: Path, project_dir: Path) -> int:
    """Copy every `src/**/*.rs` of `tree` into `project_dir`, byte for byte;
    return how many files were written. Files of `project_dir/src` the tree
    does not hold are left as they are (the workspace shell's own files)."""
    txn = TreeTxn(project_dir)
    files = _sources(tree)
    for rel, data in files.items():
        # Rust source is UTF-8, so the text round trip is the bytes.
        txn.write(rel, data.decode("utf-8"))
    txn.commit()
    return len(files)


def published_drift(tree: Path, project_dir: Path) -> tuple[str, ...]:
    """The `src/` files whose bytes in `project_dir` differ from the tree's,
    or that the tree holds and the workspace lacks. Empty when the workspace
    crate is the tree. A tree with no `src/` (no one-tree loop ran) reports
    nothing."""
    ours = _sources(tree)
    if not ours:
        return ()
    theirs = _sources(project_dir)
    return tuple(rel for rel, data in ours.items() if theirs.get(rel) != data)
