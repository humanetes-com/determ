"""Slice 9 pins — application/workspace_lifecycle.py (save → maybe-wipe → stub).

Pins the codified wipe⟺rerun invariant (session-59 wipe-vs-resume bug) and
the verbatim-moved save/wipe behavior, each wipe paired with its phase guard.
"""

from pathlib import Path

from pipeline_core.application.workspace_lifecycle import (
    may_wipe_regenerated,
    save_gen_tests_siblings,
    wipe_module_sources,
)


def _is_gen_tests_file(name: str) -> bool:
    # mirror of pipeline._is_gen_tests_file for offline tests
    stem = name[:-3] if name.endswith(".rs") else ""
    _, sep, n = stem.rpartition(".gen_tests_")
    return bool(sep) and n.isdigit()


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    src = proj / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text("pub mod error;\n")
    (src / "error.rs").write_text("pub struct E;\n")
    (src / "error.gen_tests_1.rs").write_text("#[test] fn t() {}\n")
    (proj / "tests").mkdir()
    (proj / "tests" / "it.rs").write_text("#[test] fn it() {}\n")
    # scratch + cargo dirs that must never be wiped
    (proj / ".skeleton" / "src").mkdir(parents=True)
    (proj / ".skeleton" / "src" / "error.rs").write_text("stub")
    (proj / "target" / "debug").mkdir(parents=True)
    (proj / "target" / "debug" / "gen.rs").write_text("cargo-managed")
    return proj, src


def test_wipe_invariant() -> None:
    # wipe allowed ⟺ the regenerating phase will rerun
    assert may_wipe_regenerated(regenerating_phase_done=False)
    assert not may_wipe_regenerated(regenerating_phase_done=True)
    print("  OK  wipe invariant")


def test_save_gen_tests_siblings(tmp_path: Path) -> None:
    proj, src = _tree(tmp_path)

    def fake_siblings(src_file: Path) -> list[tuple[int, Path]]:
        sib = src_file.with_name(f"{src_file.stem}.gen_tests_1.rs")
        return [(1, sib)] if sib.exists() else []

    saved = save_gen_tests_siblings(
        proj, proj, {"tests/it.rs"},
        gen_tests_siblings=fake_siblings,
        is_gen_tests_file=_is_gen_tests_file)
    # parent rel → [(pass_n, body)]; the sibling file itself is not a key
    assert saved == {"src/error.rs": [(1, "#[test] fn t() {}\n")]}
    # missing module dir → empty, no crash
    assert save_gen_tests_siblings(
        proj / "nope", proj, set(),
        gen_tests_siblings=fake_siblings,
        is_gen_tests_file=_is_gen_tests_file) == {}
    print("  OK  save gen_tests siblings")


def test_wipe_respects_phase_guard(tmp_path: Path) -> None:
    proj, src = _tree(tmp_path)
    stubbed: list[bool] = []

    # task2b done → resumable product: NOTHING wiped, no stub written.
    wiped = wipe_module_sources(
        proj, proj, {"tests/it.rs"},
        regenerating_phase_done=True,
        write_stub=lambda: stubbed.append(True))
    assert wiped == [] and not stubbed
    assert (src / "error.rs").exists()
    print("  OK  wipe refused while phase marker stands")


def test_wipe_when_phase_reruns(tmp_path: Path) -> None:
    proj, src = _tree(tmp_path)
    stubbed: list[bool] = []
    wiped = wipe_module_sources(
        proj, proj, {"tests/it.rs"},
        regenerating_phase_done=False,
        write_stub=lambda: stubbed.append(True))
    # generated sources (incl. siblings) wiped; test file, dot-dirs, target/ kept
    assert sorted(wiped) == ["src/error.gen_tests_1.rs", "src/error.rs", "src/lib.rs"]
    assert not (src / "error.rs").exists()
    assert (proj / "tests" / "it.rs").exists()
    assert (proj / ".skeleton" / "src" / "error.rs").exists()
    assert (proj / "target" / "debug" / "gen.rs").exists()
    assert stubbed == [True]    # stub written whenever the wipe branch ran
    print("  OK  wipe when phase reruns")


def test_best_tree_ratchet_roundtrip(tmp_path) -> None:
    """Ratchet: snapshot on new best, restore overwrites regressed files,
    no snapshot → no-op. Meta carries the floor pass count."""
    from pipeline_core.application.workspace_lifecycle import (
        best_tree_pass_count,
        restore_best_tree,
        save_best_tree,
    )
    assert best_tree_pass_count(tmp_path) is None
    assert restore_best_tree(tmp_path) == []
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("good body")
    (tmp_path / "src/error.rs").write_text("good error")
    n = save_best_tree(tmp_path, ["src/lib.rs", "src/error.rs", "src/ghost.rs"], 67, iteration=3)
    assert n == 2  # ghost skipped
    assert best_tree_pass_count(tmp_path) == 67
    (tmp_path / "src/lib.rs").write_text("REGRESSED")
    restored = restore_best_tree(tmp_path)
    assert sorted(restored) == ["src/error.rs", "src/lib.rs"]
    assert (tmp_path / "src/lib.rs").read_text() == "good body"
    # A higher pass count replaces the snapshot wholesale.
    (tmp_path / "src/lib.rs").write_text("better body")
    save_best_tree(tmp_path, ["src/lib.rs"], 80, iteration=4)
    assert best_tree_pass_count(tmp_path) == 80
    assert restore_best_tree(tmp_path) == ["src/lib.rs"]


def test_merged_best_tree_is_scoped_and_does_not_wipe_cluster_snapshots(tmp_path) -> None:
    """task2c had no ratchet at all — it tracked `best_pass` only to compute
    `improved`, so the stage ended on whatever its last iteration produced. It
    also cannot borrow task2a's: `save_best_tree` owns the whole `.best_tree/`
    dir and rmtree's it on every save, which would delete the per-cluster
    snapshots heal restores from."""
    from pipeline_core.application.workspace_lifecycle import (
        merged_best_pass_count,
        restore_merged_best_tree,
        save_cluster_best_tree,
        save_merged_best_tree,
    )
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    assert merged_best_pass_count(tmp_path) is None
    assert restore_merged_best_tree(tmp_path) == []

    # A cluster snapshot that must survive every merged save.
    filled = tmp_path / ".skeleton-filled" / "nightly"
    (filled / "src").mkdir(parents=True)
    (filled / "src/lib.rs").write_text("cluster body")
    save_cluster_best_tree(
        tmp_path, "nightly", filled,
        BestTreeMark(pass_count=100, iteration=6, tree_sha="abc",
                     oracle_size=104))

    (tmp_path / "src").mkdir()
    (tmp_path / "src/error.rs").write_text("good merged body")
    assert save_merged_best_tree(tmp_path, 139, iteration=2) == 1
    assert merged_best_pass_count(tmp_path) == 139
    assert (tmp_path / ".best_tree/nightly/src/lib.rs").read_text() == "cluster body"

    (tmp_path / "src/error.rs").write_text("REGRESSED")
    # Make the snapshot's mtime OLD, like a snapshot taken hours before the
    # wave: `copy2` preserves it, and cargo's freshness check then re-runs
    # the DISCARDED tree's binaries against the restored sources. Measured
    # on THIS slot 2026-08-19 (run `3579062-476abc3b`): post-merge iters 3-4
    # re-read the crashed wave's 32/2 in 1.9s after the 106-tree restore.
    import os as _os
    import time as _time
    snap = tmp_path / ".best_tree/_merged/error.rs"
    _os.utime(snap, (_time.time() - 3600, _time.time() - 3600))
    before = _time.time()
    assert restore_merged_best_tree(tmp_path) == ["src/error.rs"]
    assert (tmp_path / "src/error.rs").read_text() == "good merged body"
    assert (tmp_path / "src/error.rs").stat().st_mtime >= before - 1, (
        "the restored file kept the snapshot's mtime — invisible to cargo's "
        "fingerprint check, so the next sweep measures the discarded tree")
    assert (tmp_path / ".best_tree/nightly/src/lib.rs").exists()


def test_the_best_tree_slot_stores_and_restores_the_PAIR(tmp_path) -> None:
    """A cluster's durable best tree was write-only AS A TREE: the mark reader
    returns a score, heal's tier returns ONE file. So a re-climb that failed
    after a deliberate re-stub was permanent — measured over runs
    `1082156-4da4faf7` → `669347-cf33eae3` (2026-08-14/15), where a retrigger
    traded a reproducible 79/101 for two tests that have never passed and eight
    of the next twelve sweeps read exactly 39.

    Both halves or neither: t1.5 re-authors the layout every run, so bodies
    restored against a different skeleton do not compile."""
    from pipeline_core.application.workspace_lifecycle import (
        cluster_best_tree_has_layout,
        restore_cluster_best_tree,
        save_cluster_best_tree,
    )
    from pipeline_core.domain.best_tree_mark import BestTreeMark

    filled = tmp_path / ".skeleton-filled" / "stable"
    layout = tmp_path / ".skeleton" / "stable"
    (filled / "src").mkdir(parents=True)
    (layout / "src").mkdir(parents=True)
    (filled / "src" / "error.rs").write_text("fn good() { 79 }")
    (layout / "src" / "error.rs").write_text("fn good() { unimplemented!() }")

    saved = save_cluster_best_tree(
        tmp_path, "stable", filled,
        BestTreeMark(pass_count=79, iteration=3, tree_sha="a" * 16,
                     oracle_size=101),
        layout_dir=layout)
    assert saved == 1
    assert cluster_best_tree_has_layout(tmp_path, "stable")

    # The destruction this exists to undo, and a file created after the mark.
    (filled / "src" / "error.rs").write_text("fn bad() { 39 }")
    (layout / "src" / "error.rs").write_text("fn bad() { unimplemented!() }")
    (filled / "src" / "later.rs").write_text("fn added_after() {}")

    bodies, layout_n, leftovers = restore_cluster_best_tree(
        tmp_path, "stable", filled, layout_dir=layout)
    assert (bodies, layout_n) == (1, 1)
    assert (filled / "src" / "error.rs").read_text() == "fn good() { 79 }"
    assert (layout / "src" / "error.rs").read_text() \
        == "fn good() { unimplemented!() }"
    # Never deletes: a file the snapshot does not have is REPORTED, not removed
    # — deleting is how a restore becomes a second destruction.
    assert leftovers == ["src/later.rs"]
    assert (filled / "src" / "later.rs").exists()


def test_a_restore_bumps_mtimes_so_cargo_can_see_it(tmp_path) -> None:
    """`copy2` hands the restored file the SNAPSHOT's mtime. Cargo rebuilds
    only on newer-than-fingerprint, so a restored file that moves BACKWARDS in
    time is "fresh" and the next `cargo test` re-runs the DISCARDED tree's
    binaries against the restored sources. Run `2620196-de4a7ca2` (2026-08-17):
    the sweep 0.436s after a 57-file restore read a per-binary vector
    byte-identical to the pre-restore sweep's, stamped with the restored tree's
    sha (`4053189fa5d499d1`), and spent the once-per-process restore guard on a
    reading of the tree the restore had just discarded. Every restored file —
    bodies AND layout — must therefore come back newer than the moment the
    restore was called."""
    import os
    import time

    from pipeline_core.application.workspace_lifecycle import (
        restore_cluster_best_tree,
        save_cluster_best_tree,
    )
    from pipeline_core.domain.best_tree_mark import BestTreeMark

    filled = tmp_path / ".skeleton-filled" / "stable"
    layout = tmp_path / ".skeleton" / "stable"
    (filled / "src").mkdir(parents=True)
    (layout / "src").mkdir(parents=True)
    (filled / "src" / "error.rs").write_text("fn good() { 83 }")
    (layout / "src" / "error.rs").write_text("fn good() { unimplemented!() }")
    # The snapshot ages: the live slot's files carried 2026-08-16 21:23:34
    # while the restore ran on 2026-08-17 — a day older than every cargo
    # fingerprint of the running process.
    day_ago = time.time() - 86_400
    os.utime(filled / "src" / "error.rs", (day_ago, day_ago))
    os.utime(layout / "src" / "error.rs", (day_ago, day_ago))
    save_cluster_best_tree(
        tmp_path, "stable", filled,
        BestTreeMark(pass_count=83, iteration=2, tree_sha="a" * 16,
                     oracle_size=106),
        layout_dir=layout)

    (filled / "src" / "error.rs").write_text("fn bad() { 34 }")
    (layout / "src" / "error.rs").write_text("fn bad() { unimplemented!() }")

    t0 = time.time()
    bodies, layout_n, _ = restore_cluster_best_tree(
        tmp_path, "stable", filled, layout_dir=layout)
    assert (bodies, layout_n) == (1, 1)
    assert (filled / "src" / "error.rs").read_text() == "fn good() { 83 }"
    # 1s slack for filesystem timestamp granularity.
    assert (filled / "src" / "error.rs").stat().st_mtime >= t0 - 1, \
        "restored body kept the snapshot's mtime — cargo will not rebuild it"
    assert (layout / "src" / "error.rs").stat().st_mtime >= t0 - 1, \
        "restored layout kept the snapshot's mtime — cargo will not rebuild it"


def test_a_slot_saved_without_a_layout_declines_to_call_itself_a_pair(
        tmp_path) -> None:
    """The pre-fix slot shape. A caller must be able to ask before restoring,
    not discover it from a tree that no longer compiles."""
    from pipeline_core.application.workspace_lifecycle import (
        cluster_best_tree_has_layout,
        save_cluster_best_tree,
    )
    from pipeline_core.domain.best_tree_mark import BestTreeMark
    filled = tmp_path / ".skeleton-filled" / "stable"
    (filled / "src").mkdir(parents=True)
    (filled / "src" / "a.rs").write_text("fn a() {}")
    save_cluster_best_tree(tmp_path, "stable", filled,
                           BestTreeMark(pass_count=1, iteration=1,
                                        tree_sha="b" * 16, oracle_size=2))
    assert not cluster_best_tree_has_layout(tmp_path, "stable")
