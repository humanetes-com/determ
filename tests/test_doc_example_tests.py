"""A crate's doc examples become oracle tests the loop can work on.

Run `292191-d60a5308` (2026-10-02): the one assertion that a boxed error
still provides its backtrace is a doc example. The reconstruction lost the
behaviour and passed 401 tests; the cluster sweep cannot run the doc target
beside its own, and no route can be aimed at a doc test. Rendered as an
integration test before strip, the example passes on the original and fails
on that reconstruction. Invented docs and worlds.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.doc_example_tests import (
    DocExampleDeps,
    emit_doc_example_tests,
)
from pipeline_core.domain.doc_example_test import (
    DocExample,
    doc_example_stem,
    doc_examples,
    render_doc_example_test,
)
from pipeline_core.domain.models import DocItemRecord

_DOC = """ Sizes a gadget.

 ```
 use gadgets::size;
 # let hidden = 1;
 assert_eq!(size() + hidden, 2);
 ```

 ```toml
 not = "rust"
 ```

 ```no_run
 loop {}
 ```

 ```rust,should_panic
 panic!("boom");
 ```

 ```ignore
 nope
 ```

 ```compile_fail
 let x: u8 = "s";
 ```
"""


def test_only_the_fences_rustdoc_runs_are_examples_and_hidden_lines_run() -> None:
    found = doc_examples(_DOC)
    assert [e.should_panic for e in found] == [False, True]
    assert found[0].code == ("use gadgets::size;\nlet hidden = 1;\n"
                             "assert_eq!(size() + hidden, 2);\n")
    assert found[1].code == 'panic!("boom");\n'


def test_a_closing_fence_is_never_read_as_an_opening_one() -> None:
    doc = " ```text\n a\n ```\n ```\n let _ = 1;\n ```\n"
    assert [e.code for e in doc_examples(doc)] == ["let _ = 1;\n"]


def test_the_rendering_is_a_test_fn_with_inner_attributes_hoisted() -> None:
    text = render_doc_example_test(
        DocExample("#![feature(gadget_api)]\n\nuse gadgets::size;\nassert_eq!(size(), 1);\n"),
        fn_name="doc_lib_example_1", source="src/lib.rs (item at line 3)")
    lines = text.splitlines()
    assert lines[0] == "// determ-pipeline generated integration test"
    assert "#![feature(gadget_api)]" in lines[:6]
    assert "fn doc_example_body() -> impl DocOutcome {" in text
    assert "    use gadgets::size;\n    assert_eq!(size(), 1);" in text
    assert "#[test]\nfn doc_lib_example_1() {\n    DocOutcome::check(doc_example_body());" in text
    assert text.count("#![feature(gadget_api)]") == 1


def test_an_example_with_its_own_main_keeps_it_and_should_panic_is_an_attribute() -> None:
    text = render_doc_example_test(
        DocExample("fn helper() -> u8 { 1 }\nfn main() {\n    assert_eq!(helper(), 1);\n}\n",
                   should_panic=True),
        fn_name="doc_lib_example_2", source="src/lib.rs (item at line 9)")
    assert "\nfn main() {\n    assert_eq!(helper(), 1);\n}\n" in text
    assert "#[test]\n#[should_panic]\nfn doc_lib_example_2() {\n    DocOutcome::check(main());" in text
    assert "doc_example_body" not in text


def test_the_stem_is_the_source_file_under_a_doc_prefix() -> None:
    assert doc_example_stem("src/error.rs") == "doc_error"
    assert doc_example_stem("src/a/lib.rs") == "doc_lib"


# --- the emission -----------------------------------------------------------

_BASE = SimpleNamespace(name="base", is_default=True)
_ALT = SimpleNamespace(name="alt", is_default=False)
_GATES = {"base": "not(alt)", "alt": "alt"}


def _records() -> list[DocItemRecord]:
    return [
        DocItemRecord(file="src/lib.rs", line=3,
                      doc=" ```\n let everywhere = 1;\n ```\n ```\n let alt_only = 2;\n ```\n"),
        DocItemRecord(file="src/lib.rs", line=20, doc=" ```\n let nowhere = 3;\n ```\n"),
        DocItemRecord(file="tests/t.rs", line=1, doc=" ```\n let not_src = 4;\n ```\n"),
    ]


def _deps(log: list[str], offered: list[tuple[str, str]],
          measurable: tuple[str, ...] = ("base", "alt")) -> DocExampleDeps:
    def _settle(_p: Path, _m: str, f: Path, block: str, c: SimpleNamespace) -> str | None:
        """`everywhere` passes in both worlds, `alt_only` only under alt,
        `nowhere` in none — as the real settle would report them."""
        offered.append((f.name, c.name))
        assert f.read_text() == f"#![cfg({_GATES[c.name]})]\n" + block
        if "everywhere" in block:
            f.write_text(block)
            return ""
        if "alt_only" in block:
            return _GATES["alt"] if c.name == "alt" else None
        return None

    return DocExampleDeps(
        enumerate_doc_items=lambda _p: _records(),
        world_gate=lambda c: _GATES[c.name], settle=_settle,
        measurable=lambda c: c.name in measurable, log=log.append)


def test_each_example_is_offered_default_first_and_kept_where_it_passes(
        tmp_path: Path) -> None:
    """The plan's sentence: an example that passes on the original is an
    oracle test in the worlds it passes in; one that passes in none is not
    in the oracle. NEGATIVE CONTROL: before this step `tests/` held none of
    them, so no sweep could show one red."""
    log: list[str] = []
    offered: list[tuple[str, str]] = []
    assert not (tmp_path / "tests").exists()
    reading = emit_doc_example_tests(tmp_path, "gadgets", [_ALT, _BASE],
                                     _deps(log, offered))
    assert reading.examples == 3            # the tests/ record is not a src doc
    assert reading.kept == ("doc_lib_gen_1.rs", "doc_lib_gen_2.rs")
    assert reading.dropped == ("src/lib.rs:20 #0",)
    assert offered == [
        ("doc_lib_gen_1.rs", "base"),
        ("doc_lib_gen_2.rs", "base"), ("doc_lib_gen_2.rs", "alt"),
        ("doc_lib_gen_3.rs", "base"), ("doc_lib_gen_3.rs", "alt")]
    tests = tmp_path / "tests"
    assert sorted(p.name for p in tests.iterdir()) == [
        "doc_lib_gen_1.rs", "doc_lib_gen_2.rs"]
    assert (tests / "doc_lib_gen_1.rs").read_text().startswith(
        "// determ-pipeline generated integration test")
    assert (tests / "doc_lib_gen_2.rs").read_text().startswith("#![cfg(alt)]\n")
    assert "fn doc_lib_example_2()" in (tests / "doc_lib_gen_2.rs").read_text()
    assert "2 pass on the original as tests" in log[-1] and "1 dropped" in log[-1]


def test_a_re_entry_replaces_its_own_files_and_leaves_others_alone(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "doc_lib_gen_7.rs").write_text(
        "// determ-pipeline generated integration test\n#[test]\nfn stale() {}\n")
    (tests / "doc_notes_gen_1.rs").write_text("#[test]\nfn authored() {}\n")
    emit_doc_example_tests(tmp_path, "gadgets", [_BASE, _ALT], _deps([], []))
    names = sorted(p.name for p in tests.iterdir())
    assert "doc_lib_gen_7.rs" not in names          # ours, from an earlier pass
    assert "doc_notes_gen_1.rs" in names            # authored: no origin marker


def test_an_example_is_never_offered_to_a_world_that_cannot_verify_it(
        tmp_path: Path) -> None:
    offered: list[tuple[str, str]] = []
    reading = emit_doc_example_tests(
        tmp_path, "gadgets", [_BASE, _ALT], _deps([], offered, measurable=("base",)))
    assert {w for _f, w in offered} == {"base"}
    assert reading.kept == ("doc_lib_gen_1.rs",)    # `alt_only` has no verifier


def test_no_records_or_no_cluster_emits_nothing(tmp_path: Path) -> None:
    deps = _deps([], [])
    assert emit_doc_example_tests(tmp_path, "g", [], deps).examples == 0
    import dataclasses
    none = dataclasses.replace(deps, enumerate_doc_items=lambda _p: None)
    assert emit_doc_example_tests(tmp_path, "g", [_BASE], none).examples == 0
    assert not (tmp_path / "tests").exists()
