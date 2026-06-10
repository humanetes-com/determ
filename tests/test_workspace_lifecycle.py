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
