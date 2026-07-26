"""Pins for the on-disk repair of self-redeclaring (hollow) fn bodies."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.hollow_repair import repair_hollow_bodies

_HOLLOW = """\
unsafe fn drop_rest<C>(e: Own<Err>, target: TypeId)
where
    C: 'static,
{
    unsafe fn drop_rest<C>(e: Own<Err>, target: TypeId)
    where
        C: 'static,
    {
        ptr::write(e.ptr.as_ptr(), target);
    }
}
"""


def _tree(tmp_path: Path, **files: str) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, text in files.items():
        (src / f"{name}.rs").write_text(text, encoding="utf-8")
    return src


def test_repair_un_nests_the_body_and_keeps_the_work(tmp_path: Path) -> None:
    src = _tree(tmp_path, error=_HOLLOW)
    reps = repair_hollow_bodies(src, lambda _p: True)
    assert [(r.file, r.fn_name, r.is_test) for r in reps] == [
        ("error.rs", "drop_rest", False)]
    text = (src / "error.rs").read_text()
    assert text.count("fn drop_rest") == 1               # the nested item is gone
    assert "ptr::write(e.ptr.as_ptr(), target);" in text  # the work is kept


def test_repair_drops_the_surplus_brace_only_when_that_is_what_parses(
        tmp_path: Path) -> None:
    # The whole-item reply carries its own closing brace; it lands just past the
    # outer fn and is the syntax error that kills the whole cluster. rustc names
    # ONE unparseable file per run, so an un-repaired one hides every other.
    src = _tree(tmp_path, error=_HOLLOW.rstrip("\n") + "\n}\n")

    def parses(p: Path) -> bool:
        return p.read_text().count("{") == p.read_text().count("}")

    reps = repair_hollow_bodies(src, parses)
    assert [r.dropped_stray_brace for r in reps] == [True]
    assert parses(src / "error.rs")


def test_repair_flags_a_hollow_test_that_was_passing_without_asserting(
        tmp_path: Path) -> None:
    # A hollow `#[test]` passes while asserting nothing, and test targets are
    # immutable to the fill loop — this pass is its ONLY repair path.
    src = _tree(tmp_path, fmt=(
        "#[cfg(test)]\nmod tests {\n    #[test]\n    fn no_digits() {\n"
        "        fn no_digits() {\n            assert_eq!(1, 2);\n        }\n"
        "    }\n}\n"))
    reps = repair_hollow_bodies(src, lambda _p: True)
    assert [(r.fn_name, r.is_test) for r in reps] == [("no_digits", True)]
    assert "assert_eq!(1, 2);" in (src / "fmt.rs").read_text()


def test_repair_rolls_back_rather_than_break_a_file_that_parsed(
        tmp_path: Path) -> None:
    src = _tree(tmp_path, error=_HOLLOW)
    before = (src / "error.rs").read_text()
    # A validator that accepts the original and rejects everything else.
    assert repair_hollow_bodies(src, lambda p: p.read_text() == before) == []
    assert (src / "error.rs").read_text() == before


def test_repair_leaves_ordinary_and_recursive_helper_bodies_alone(
        tmp_path: Path) -> None:
    src = _tree(
        tmp_path,
        a="fn go() -> i32 {\n    1 + 1\n}\n",
        # A same-named nested helper that is actually CALLED is legal and works.
        b="fn go() -> i32 {\n    fn go() -> i32 { 1 }\n    go()\n}\n",
    )
    assert repair_hollow_bodies(src, lambda _p: True) == []
