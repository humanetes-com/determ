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
    assert restore_merged_best_tree(tmp_path) == ["src/error.rs"]
    assert (tmp_path / "src/error.rs").read_text() == "good merged body"
    assert (tmp_path / ".best_tree/nightly/src/lib.rs").exists()
