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


def save_cluster_best_tree(
    project_dir: Path, cluster: str, filled_dir: Path, pass_count: int,
    iteration: int = 0,
) -> int:
    """Snapshot a cluster's fill tree (``.skeleton-filled/<cluster>/``) into
    ``.best_tree/<cluster>/`` so heal's BEST_TREE_COPY tier has a parseable
    good-fill copy of each file to restore from. Layout mirrors what
    ``heal``'s ``best_tree_bytes(rel)`` reads: ``.best_tree/<cluster>/<rel>``
    with ``rel`` relative to ``filled_dir`` (e.g. ``src/error.rs``).

    Cluster-scoped: replaces only this cluster's subtree, never other
    clusters' snapshots. Returns files saved."""
    import json
    import shutil
    dst_root = project_dir / BEST_TREE_DIR / cluster
    if dst_root.exists():
        shutil.rmtree(dst_root)
    saved = 0
    for src in sorted(Path(filled_dir).rglob("*.rs")):
        if not src.is_file():
            continue
        dst = dst_root / src.relative_to(filled_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        saved += 1
    if saved:
        (dst_root / _BEST_META).write_text(
            json.dumps({"pass_count": pass_count, "iteration": iteration}),
            encoding="utf-8")
    return saved


# The merged tree gets its own slot under the same root. It cannot use
# `save_best_tree`/`restore_best_tree`: those own the WHOLE `.best_tree/` dir
# and `rmtree` it on every save, which would delete the per-cluster snapshots
# heal restores from. `_merged` is a name no cluster can take — cluster names
# come from the baked cfg scan and are cfg atoms.
MERGED_BEST = "_merged"


def save_merged_best_tree(project_dir: Path, pass_count: int,
                          iteration: int = 0) -> int:
    """Snapshot the merged `src/` at its best. task2c tracked `best_pass` only
    to compute `improved`; nothing kept the tree that earned it, so the stage
    ended on whatever its last iteration produced. task2a has had this ratchet
    all along."""
    return save_cluster_best_tree(project_dir, MERGED_BEST, project_dir / "src",
                                  pass_count, iteration)


def merged_best_pass_count(project_dir: Path) -> int | None:
    """Pass count of the merged snapshot, or None if there is none."""
    import json
    meta = project_dir / BEST_TREE_DIR / MERGED_BEST / _BEST_META
    try:
        return int(json.loads(meta.read_text(encoding="utf-8"))["pass_count"])
    except (OSError, ValueError, KeyError):
        return None


def restore_merged_best_tree(project_dir: Path) -> list[str]:
    """Copy the merged snapshot back over `src/`. Returns restored rel paths."""
    best = project_dir / BEST_TREE_DIR / MERGED_BEST
    if not best.is_dir():
        return []
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
