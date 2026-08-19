"""Module-start workspace lifecycle (slice 9): save siblings → (maybe) wipe → stub.

Codifies THE invariant that broke in session 59 (the wipe-vs-resume bug,
commit 9e182bb): a startup wipe of regenerated sources is allowed ⟺ the
phase that REGENERATES the wiped artefact will rerun. Wiping while the
regenerating phase's done-marker stands leaves later phases running against
a stub tree (the t2c→t3 fall-through). :func:`may_wipe_regenerated` is that
invariant as code; :func:`wipe_module_sources` refuses to wipe unless it
holds, so a call site cannot wipe without stating the phase guard.

Bodies moved verbatim from pipeline.py's module loop. Filename regex stays
in pipeline.py behind the injected ``is_gen_tests_file`` predicate (Group-E
convention); logging goes through the ambient log facade.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path

from pipeline_core.domain.best_tree_mark import UNKNOWN_TREE_SHA, BestTreeMark
from pipeline_core.log import log


def may_wipe_regenerated(*, regenerating_phase_done: bool) -> bool:
    """The wipe⟺rerun invariant: wiping an artefact is safe only when the
    phase that regenerates it will rerun (its done-marker is NOT set)."""
    return not regenerating_phase_done


def save_gen_tests_siblings(
    module_dir: Path,
    project_dir: Path,
    existing_test_files: Collection[str],
    *,
    gen_tests_siblings: Callable[[Path], list[tuple[int, Path]]],
    is_gen_tests_file: Callable[[str], bool],
) -> dict[str, list[tuple[int, str]]]:
    """Save gen_tests siblings + their declarations BEFORE wiping.

    TASK 0c writes test code to foo.gen_tests_N.rs siblings; TASK 2
    regenerates source files. The caller reattaches siblings + decls after
    each TASK 2 write so TASK 3 can actually run the tests.

    Returns {rel_path of parent → list of (pass_n, sibling_body)}.
    """
    saved_gen_blocks: dict[str, list[tuple[int, str]]] = {}
    if module_dir.exists():
        for src_file in sorted(module_dir.rglob("*.rs")):
            rel = str(src_file.relative_to(project_dir))
            if rel in existing_test_files:
                continue
            if is_gen_tests_file(src_file.name):
                continue  # siblings handled via their parent
            try:
                siblings = gen_tests_siblings(src_file)
                if siblings:
                    saved_gen_blocks[rel] = [
                        (n, p.read_text(encoding="utf-8"))
                        for n, p in siblings
                    ]
            except Exception:
                pass
    if saved_gen_blocks:
        _total = sum(len(v) for v in saved_gen_blocks.values())
        log(f"  Saved {_total} gen_tests sibling(s) from "
            f"{len(saved_gen_blocks)} file(s) for reattachment.")
    return saved_gen_blocks


# Never wiped: cargo's build output (contains dependency build-script outputs
# like target/debug/build/thiserror-xxx/out/private.rs that cargo expects on
# subsequent compiles — deleting these breaks dependency compilation in
# mysterious ways). Dot-dirs (.skeleton/, .skeleton-filled/, .task1_partials/,
# .cluster_augmented/, …) are skipped by the component check below.
_WIPE_SKIP_DIRS = frozenset({"target"})


def wipe_module_sources(
    module_dir: Path,
    project_dir: Path,
    existing_test_files: Collection[str],
    *,
    regenerating_phase_done: bool,
    write_stub: Callable[[], object],
) -> list[str]:
    """Wipe previously generated files for a module (interrupted run) — ONLY
    when :func:`may_wipe_regenerated` holds for the regenerating phase
    (task2b merge). When task2b will rerun it regenerates src/ from
    .skeleton-filled/, so wiping is safe; when task2b IS done, src/ is the
    resumable product and t2c's fills live only there.

    Only regenerated source files are wiped — never scratch/cache dot-dirs
    or anything cargo manages. After a wipe pass, ``write_stub`` is invoked
    so the workspace still parses while regenerating.

    Returns the wiped rel-paths ([] when the invariant forbade the wipe).
    """
    if not module_dir.exists() or not may_wipe_regenerated(
            regenerating_phase_done=regenerating_phase_done):
        return []
    wiped: list[str] = []
    for src_file in sorted(module_dir.rglob("*.rs")):
        rel = str(src_file.relative_to(project_dir))
        if rel in existing_test_files:
            continue
        # Skip scratch dirs (any component starting with '.') and
        # cargo's target/ tree.
        parts = src_file.parts
        if any(p.startswith(".") for p in parts):
            continue
        if any(p in _WIPE_SKIP_DIRS for p in parts):
            continue
        src_file.unlink()
        wiped.append(rel)
    if wiped:
        log(f"  Wiped {len(wiped)} leftover generated file(s): {', '.join(wiped)}")
    # Stub so workspace parses while we generate.
    write_stub()
    return wiped


# --- Best-tree ratchet (2026-06-11, no-wipe repair iterations) ---------------
# Iterations repair the kept tree instead of wiping (R13/RepoSpec). The
# ratchet bounds cross-iteration damage: snapshot the source tree whenever
# the t3 pass count sets a new best; if an iteration ends BELOW the best
# (regressed fix, broken build → pass count 0), restore the best tree before
# the next iteration. Repair may experiment but can never end below the
# floor — the outer-loop analog of FloorStall.

BEST_TREE_DIR = ".best_tree"
_BEST_META = "_meta.json"
# Where the authored layout half of a cluster snapshot lives.
_BEST_LAYOUT = "_layout"


def best_tree_pass_count(project_dir: Path) -> int | None:
    """Pass count of the snapshotted best tree, or None if no snapshot."""
    import json
    meta = project_dir / BEST_TREE_DIR / _BEST_META
    try:
        return int(json.loads(meta.read_text(encoding="utf-8"))["pass_count"])
    except (OSError, ValueError, KeyError):
        return None


def save_best_tree(
    project_dir: Path, rel_files: Collection[str], pass_count: int,
    iteration: int = 0,
) -> int:
    """Snapshot `rel_files` (source files, tests excluded by the caller) into
    .best_tree/ with their pass count. Returns files saved."""
    import json
    import shutil
    best = project_dir / BEST_TREE_DIR
    if best.exists():
        shutil.rmtree(best)
    saved = 0
    for rel in rel_files:
        src = project_dir / rel
        if not src.is_file():
            continue
        dst = best / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved += 1
    if saved:
        best.mkdir(exist_ok=True)
        (best / _BEST_META).write_text(
            json.dumps({"pass_count": pass_count, "iteration": iteration}),
            encoding="utf-8")
        log(f"  [ratchet] best tree snapshotted: {saved} file(s), "
            f"{pass_count} passing (iter {iteration}).")
    return saved


def load_cluster_best_tree_mark(
    project_dir: Path, cluster: str,
) -> BestTreeMark | None:
    """What ``.best_tree/<cluster>/`` says about itself, or None if no snapshot.

    The reader the TDD driver seeds its replace-decision from at loop entry.
    Without it that decision starts every process believing the slot is empty,
    and the first sweep replaces whatever is there — see
    ``domain/best_tree_mark.py`` for the run that measured it."""
    import json
    meta = project_dir / BEST_TREE_DIR / cluster / _BEST_META
    try:
        return BestTreeMark.from_dict(json.loads(meta.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def save_cluster_best_tree(
    project_dir: Path, cluster: str, filled_dir: Path, mark: BestTreeMark,
    layout_dir: Path | None = None,
) -> int:
    """Snapshot a cluster's fill tree (``.skeleton-filled/<cluster>/``) into
    ``.best_tree/<cluster>/`` so heal's BEST_TREE_COPY tier has a parseable
    good-fill copy of each file to restore from. Layout mirrors what
    ``heal``'s ``best_tree_bytes(rel)`` reads: ``.best_tree/<cluster>/<rel>``
    with ``rel`` relative to ``filled_dir`` (e.g. ``src/error.rs``).

    Cluster-scoped: replaces only this cluster's subtree, never other
    clusters' snapshots. Returns files saved.

    Destructive by construction — the slot is replaced wholesale, so a caller
    that has not established ``mark`` beats what is already stored destroys it.
    ``load_cluster_best_tree_mark`` is how that is established.

    STAGED AND SWAPPED, because the obvious order was unsafe in a way that
    defeated exactly the guard above. It used to rmtree the slot, copy the files
    one at a time, and write ``_meta.json`` LAST — so a death anywhere in that
    window (the process this slot exists to survive) left the slot populated and
    MARK-LESS. ``load_cluster_best_tree_mark`` then returns None, the driver
    seeds ``_checkpoint_sha`` to None, and ``snapshot_due``'s first clause
    (``snap_sha is None`` — "there is no snapshot yet") is true of the variable
    and false of the directory, so the next process's first sweep claimed the
    slot however much worse it was. That is the same shape as the crash the
    atomic write in ``_save_tdd_floor`` exists for.

    So: build the replacement beside the slot with its mark already inside, then
    two renames. Both are atomic, the slot is never half-written, and the worst
    a death leaves behind is a ``<cluster>.old-<pid>`` directory that is named
    for what it is rather than a silent downgrade."""
    import json
    import os as _os
    import shutil
    root = project_dir / BEST_TREE_DIR
    dst_root = root / cluster
    staging = root / f"{cluster}.staging-{_os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    saved = 0
    for src in sorted(Path(filled_dir).rglob("*.rs")):
        if not src.is_file():
            continue
        dst = staging / src.relative_to(filled_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved += 1
    # The LAYOUT half, under `_layout/`. Bodies from one run do not compile
    # against another run's skeleton — t1.5 re-authors it every run, and
    # between two 2026-08-14 commits `ErrorImpl` lost a whole field — so a
    # snapshot of the filled tree alone is not a restorable tree, only a
    # per-file donor for heal. Storing the pair is what makes
    # `restore_cluster_best_tree` possible without going to the object store.
    # `_layout` cannot collide with a restored path: the filled tree's files
    # all live under `src/`.
    if layout_dir is not None and Path(layout_dir).is_dir():
        for src in sorted(Path(layout_dir).rglob("*.rs")):
            if not src.is_file():
                continue
            dst = staging / _BEST_LAYOUT / src.relative_to(layout_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if not saved:
        # Nothing to store. Leave whatever is already in the slot alone: an
        # empty fill tree is not evidence that the stored one is stale.
        if staging.exists():
            shutil.rmtree(staging)
        return 0
    # The mark goes in BEFORE the swap, so the slot is mark-less at no point.
    (staging / _BEST_META).write_text(json.dumps(mark.to_dict()), encoding="utf-8")
    retired = root / f"{cluster}.old-{_os.getpid()}"
    if dst_root.exists():
        _os.replace(dst_root, retired)
    try:
        _os.replace(staging, dst_root)
    except OSError:
        if retired.exists():          # put the old slot back rather than none
            _os.replace(retired, dst_root)
        raise
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)
    return saved


def restore_cluster_best_tree(
    project_dir: Path, cluster: str, filled_dir: Path,
    layout_dir: Path | None = None,
) -> tuple[int, int, list[str]]:
    """Put a cluster's durable best tree back — BOTH halves — and report what
    it cost. Returns (bodies restored, layout files restored, leftovers).

    The absence this fills. `.best_tree/<cluster>` had exactly two readers and
    neither could return the tree: `load_cluster_best_tree_mark` reads the
    score to decide whether a sweep may REPLACE the slot, and heal's
    `BEST_TREE_COPY` tier reads ONE file when that file is unparseable.
    `restore_best_tree` exists but is module scope over the whole `.best_tree/`
    dir inside a task3/task4 repair. So a cluster's durable best tree was
    write-only AS A TREE, and a re-climb that failed after a deliberate
    re-stub was permanent by construction: measured over runs
    `1082156-4da4faf7` → `669347-cf33eae3` (2026-08-14/15), the retrigger
    traded a reproducible 79/101 for two tests that have never passed in any
    run, eight of the next twelve scored sweeps read exactly 39, and nothing in
    the loop could take the 79 back.

    Restores by OVERWRITE and never deletes: a `.rs` file the live tree has and
    the snapshot does not is reported as a leftover for the caller to log, not
    removed. Deleting is how a restore turns into a second destruction, and the
    leftover case (t1.5 added a file after the snapshot) is rare enough to be
    worth a line in the log instead."""
    import os as _os
    import shutil
    best = project_dir / BEST_TREE_DIR / cluster
    if not best.is_dir():
        return 0, 0, []
    bodies = 0
    stored: set[Path] = set()
    for src in sorted(best.rglob("*.rs")):
        if not src.is_file():
            continue
        rel = src.relative_to(best)
        if rel.parts and rel.parts[0] == _BEST_LAYOUT:
            continue
        dst = Path(filled_dir) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # `copy2` hands the restored file the SNAPSHOT's mtime, which is older
        # than the cargo fingerprints this run's own fill compiles recorded —
        # and cargo rebuilds only on newer-than-fingerprint, so without the
        # bump the next `cargo test` re-runs the DISCARDED tree's binaries
        # against the restored sources. Run `2620196-de4a7ca2` (2026-08-17):
        # the sweep 0.4s after a 57-file restore read a per-binary vector
        # byte-identical to the pre-restore sweep's, stamped with the restored
        # tree's sha, and spent the once-per-process restore guard on it.
        _os.utime(dst)
        stored.add(rel)
        bodies += 1
    layout = 0
    layout_root = best / _BEST_LAYOUT
    if layout_dir is not None and layout_root.is_dir():
        for src in sorted(layout_root.rglob("*.rs")):
            if not src.is_file():
                continue
            dst = Path(layout_dir) / src.relative_to(layout_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            _os.utime(dst)  # same freshness bump as the bodies half above
            layout += 1
    leftovers = sorted(
        str(p.relative_to(filled_dir))
        for p in Path(filled_dir).rglob("*.rs")
        if p.is_file() and p.relative_to(filled_dir) not in stored)
    return bodies, layout, leftovers


def cluster_best_tree_has_layout(project_dir: Path, cluster: str) -> bool:
    """Whether the slot holds the LAYOUT half, i.e. whether a restore of it
    would be a pair. A slot written before the layout half was stored holds
    bodies only, and restoring those alone gives a tree that does not compile
    — the caller must be able to decline rather than discover it afterwards."""
    return (project_dir / BEST_TREE_DIR / cluster / _BEST_LAYOUT).is_dir()


# The merged tree gets its own slot under the same root. It cannot use
# `save_best_tree`/`restore_best_tree`: those own the WHOLE `.best_tree/` dir
# and `rmtree` it on every save, which would delete the per-cluster snapshots
# heal restores from. `_merged` is a name no cluster can take — cluster names
# come from the baked cfg scan and are cfg atoms.
MERGED_BEST = "_merged"


def save_merged_best_tree(project_dir: Path, pass_count: int,
                          iteration: int = 0, tree_sha: str = UNKNOWN_TREE_SHA,
                          oracle_size: int = 0) -> int:
    """Snapshot the merged `src/` at its best. task2c tracked `best_pass` only
    to compute `improved`; nothing kept the tree that earned it, so the stage
    ended on whatever its last iteration produced. task2a has had this ratchet
    all along."""
    return save_cluster_best_tree(
        project_dir, MERGED_BEST, project_dir / "src",
        BestTreeMark(pass_count=pass_count, iteration=iteration,
                     tree_sha=tree_sha, oracle_size=oracle_size))


def merged_best_pass_count(project_dir: Path) -> int | None:
    """Pass count of the merged snapshot, or None if there is none."""
    mark = load_cluster_best_tree_mark(project_dir, MERGED_BEST)
    return None if mark is None else mark.pass_count


def restore_merged_best_tree(project_dir: Path) -> list[str]:
    """Copy the merged snapshot back over `src/`. Returns restored rel paths."""
    best = project_dir / BEST_TREE_DIR / MERGED_BEST
    if not best.is_dir():
        return []
    import os as _os
    import shutil
    restored: list[str] = []
    for src in sorted(best.rglob("*.rs")):
        if not src.is_file():
            continue
        # Project-relative, like `restore_best_tree` — the snapshot is rooted
        # at `src/` but every caller and log line speaks in project paths.
        rel = Path("src") / src.relative_to(best)
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # Same freshness bump as `restore_cluster_best_tree`: `copy2` hands
        # the file the snapshot's mtime, older than cargo's fingerprints, so
        # without it the next `cargo test` re-runs the DISCARDED tree's
        # binaries. Measured on THIS slot 2026-08-19 (run `3579062-476abc3b`):
        # after the 106-tree restore at post-merge iter 2, iters 3 and 4 both
        # re-read the crashed wave's 32/2 in 1.9s.
        _os.utime(dst)
        restored.append(str(rel))
    if restored:
        log(f"  [ratchet] restored merged best tree ({len(restored)} file(s), "
            f"{merged_best_pass_count(project_dir)} passing) — iteration ended "
            "below the floor.")
    return restored


def restore_best_tree(project_dir: Path) -> list[str]:
    """Copy the snapshotted best tree back over the working tree. Returns the
    restored relative paths ([] if no snapshot)."""
    best = project_dir / BEST_TREE_DIR
    if not best.is_dir():
        return []
    import shutil
    restored: list[str] = []
    for src in sorted(best.rglob("*")):
        if not src.is_file() or src.name == _BEST_META:
            continue
        rel = str(src.relative_to(best))
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(rel)
    if restored:
        log(f"  [ratchet] restored best tree ({len(restored)} file(s), "
            f"{best_tree_pass_count(project_dir)} passing) — iteration ended below the floor.")
    return restored
