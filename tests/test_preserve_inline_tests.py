"""Pins for the pre-strip relocation of inline unit tests into gen_tests siblings."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.discovery import is_gen_tests_file
from pipeline_core.adapters.gen_tests_storage import gen_tests_path, next_gen_tests_n
from pipeline_core.application.preserve_inline_tests import (
    PreserveInlineTestsDeps,
    preserve_inline_tests,
)
from pipeline_core.domain.generation import build_gen_tests_decl

_FILE = (
    "pub fn indent() {}\n"
    "#[cfg(test)]\n"
    "mod tests {\n"
    "    use super::*;\n"
    "    #[test]\n"
    "    fn no_digits() {\n"
    "        assert_eq!(indent(), ());\n"
    "    }\n"
    "}\n"
)


def _deps(logs: list[str]) -> PreserveInlineTestsDeps:
    return PreserveInlineTestsDeps(
        log=logs.append,
        next_gen_tests_n=next_gen_tests_n,
        gen_tests_path=gen_tests_path,
        build_gen_tests_decl=build_gen_tests_decl,
        derive_gentest_prelude=lambda _p: ("#[allow(unused)]", ""),
        is_gen_tests_file=is_gen_tests_file,
    )


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "src").mkdir()
    for name, text in files.items():
        (tmp_path / "src" / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_moves_inline_tests_into_a_sibling(tmp_path: Path) -> None:
    p = _project(tmp_path, {"fmt.rs": _FILE})
    logs: list[str] = []
    res = preserve_inline_tests(p / "src", p, _deps(logs))
    assert res.moved == 1 and res.refused == 0 and res.files == ("fmt.rs",)
    sibling = p / "src" / "fmt.gen_tests_1.rs"
    assert "fn no_digits()" in sibling.read_text()
    assert "use super::*;" in sibling.read_text()


def test_parent_keeps_a_path_decl_where_the_block_was(tmp_path: Path) -> None:
    """In place, not appended: the module position is what makes `super::*`
    resolve to the same parent it did inline."""
    p = _project(tmp_path, {"fmt.rs": _FILE})
    preserve_inline_tests(p / "src", p, _deps([]))
    out = (p / "src" / "fmt.rs").read_text()
    assert "mod tests {" not in out
    assert "fn no_digits" not in out
    assert 'mod gen_tests_1;' in out
    assert '#[path = "fmt.gen_tests_1.rs"]' in out
    assert out.index("pub fn indent") < out.index("mod gen_tests_1;")


def test_is_idempotent(tmp_path: Path) -> None:
    """A path-decl is not an inline mod, so a second pass finds nothing — the
    step is safe on a resumed run that already relocated its tests."""
    p = _project(tmp_path, {"fmt.rs": _FILE})
    preserve_inline_tests(p / "src", p, _deps([]))
    first = (p / "src" / "fmt.rs").read_text()
    assert preserve_inline_tests(p / "src", p, _deps([])).moved == 0
    assert (p / "src" / "fmt.rs").read_text() == first
    assert not (p / "src" / "fmt.gen_tests_2.rs").exists()


def test_numbering_continues_past_existing_siblings(tmp_path: Path) -> None:
    p = _project(tmp_path, {
        "fmt.rs": '#[cfg(test)]\n#[path = "fmt.gen_tests_1.rs"]\nmod gen_tests_1;\n' + _FILE,
        "fmt.gen_tests_1.rs": "#[test]\nfn augmented() {}\n",
    })
    preserve_inline_tests(p / "src", p, _deps([]))
    assert (p / "src" / "fmt.gen_tests_2.rs").exists()
    assert (p / "src" / "fmt.gen_tests_1.rs").read_text() == "#[test]\nfn augmented() {}\n"


def test_two_blocks_number_in_source_order(tmp_path: Path) -> None:
    p = _project(tmp_path, {"m.rs": (
        "#[cfg(test)]\nmod a { #[test] fn x() {} }\n"
        "#[cfg(test)]\nmod b { #[test] fn y() {} }\n")})
    preserve_inline_tests(p / "src", p, _deps([]))
    assert "fn x()" in (p / "src" / "m.gen_tests_1.rs").read_text()
    assert "fn y()" in (p / "src" / "m.gen_tests_2.rs").read_text()


def test_extra_cfg_gate_is_carried_onto_the_decl(tmp_path: Path) -> None:
    p = _project(tmp_path, {"m.rs": (
        '#[cfg(feature = "std")]\n#[cfg(test)]\nmod tests { #[test] fn a() {} }\n')})
    preserve_inline_tests(p / "src", p, _deps([]))
    assert '#[cfg(feature="std")]' in (p / "src" / "m.rs").read_text()


def test_conjunctive_test_gate_is_carried_onto_the_decl(tmp_path: Path) -> None:
    p = _project(tmp_path, {"m.rs": (
        '#[cfg(all(test, feature = "x"))]\nmod tests { #[test] fn a() {} }\n')})
    assert preserve_inline_tests(p / "src", p, _deps([])).moved == 1
    assert '#[cfg(feature="x")]' in (p / "src" / "m.rs").read_text()


def test_refusal_leaves_the_block_inline_and_says_so(tmp_path: Path) -> None:
    """A test left inline is the exact state this step exists to remove, so it
    must be reported — never dropped silently."""
    p = _project(tmp_path, {"m.rs": (
        '#[cfg(any(test, feature = "x"))]\nmod tests { #[test] fn a() {} }\n')})
    logs: list[str] = []
    res = preserve_inline_tests(p / "src", p, _deps(logs))
    assert res.moved == 0 and res.refused == 1
    assert "mod tests {" in (p / "src" / "m.rs").read_text()
    assert any("left INLINE" in m and "does not require test" in m for m in logs)


def test_existing_siblings_are_not_rescanned(tmp_path: Path) -> None:
    """A sibling's own body is test code by construction; treating it as a
    source file would relocate it into a sibling of itself."""
    p = _project(tmp_path, {
        "m.rs": "pub fn f() {}\n",
        "m.gen_tests_1.rs": "#[cfg(test)]\nmod inner { #[test] fn a() {} }\n"})
    assert preserve_inline_tests(p / "src", p, _deps([])).moved == 0


def test_file_without_tests_is_untouched(tmp_path: Path) -> None:
    p = _project(tmp_path, {"m.rs": "pub fn f() {}\n"})
    preserve_inline_tests(p / "src", p, _deps([]))
    assert (p / "src" / "m.rs").read_text() == "pub fn f() {}\n"
