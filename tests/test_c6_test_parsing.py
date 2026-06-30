"""C6: test-parsing/discovery helpers relocated from pipeline.py."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.test_locator import find_tests_referencing_fn
from pipeline_core.domain.generation import GEN_PRELUDE_ITEMS, inject_gen_prelude_imports
from pipeline_core.domain.rust_macros import proc_macro_fallback_requirements


def test_find_tests_referencing_fn_ranks_by_count(tmp_path: Path) -> None:
    a = tmp_path / "a.rs"
    b = tmp_path / "b.rs"
    c = tmp_path / "c.rs"
    a.write_text("foo(); foo(); foo();", encoding="utf-8")
    b.write_text("foo();", encoding="utf-8")
    c.write_text("bar();", encoding="utf-8")
    out = find_tests_referencing_fn([a, b, c], "foo")
    assert [Path(p).name for p, _ in out] == ["a.rs", "b.rs"]  # c omitted, ranked


def test_find_tests_referencing_fn_empty_name() -> None:
    assert find_tests_referencing_fn([], "") == []


def test_find_tests_referencing_fn_caps_blocks(tmp_path: Path) -> None:
    files = []
    for i in range(6):
        p = tmp_path / f"f{i}.rs"
        p.write_text("foo();", encoding="utf-8")
        files.append(p)
    assert len(find_tests_referencing_fn(files, "foo", max_blocks=2)) == 2


def test_inject_gen_prelude_after_super_glob() -> None:
    block = "mod gen_tests_1 {\n    use super::*;\n    fn t() {}\n}\n"
    out = inject_gen_prelude_imports(block)
    assert "use super::*;" in out
    assert "use std::vec::Vec;" in out
    # Already-present imports are not duplicated.
    assert out.count("use std::vec::Vec;") == 1


def test_inject_gen_prelude_noop_when_all_present() -> None:
    have = "\n    ".join(stmt for stmt, _, _ in GEN_PRELUDE_ITEMS)
    block = f"mod gen_tests_1 {{\n    {have}\n}}\n"
    assert inject_gen_prelude_imports(block) == block


def test_inject_gen_prelude_no_mod_opener_returns_unchanged() -> None:
    block = "fn loose() {}\n"
    assert inject_gen_prelude_imports(block) == block


def test_proc_macro_fallback_derives_and_attrs() -> None:
    relevant = [("t.rs", '#[derive(Debug, Clone)]\n#[my_crate(skip)]\nstruct X;')]
    out = proc_macro_fallback_requirements(relevant, "my-crate")
    assert "#[derive(Debug)]" in out and "#[derive(Clone)]" in out
    assert "Helper attributes" in out and "#[my_crate(skip)]" in out
    assert "proc_macro_derive" in out


def test_proc_macro_fallback_no_attrs_section_when_none() -> None:
    # With a crate_name set, only `#[my_crate(...)]` (not `derive`) feeds the
    # helper-attribute section, so a derive-only input has none.
    out = proc_macro_fallback_requirements(
        [("t.rs", "#[derive(Debug)] struct X;")], "my-crate")
    assert "Helper attributes" not in out
    assert "#[derive(Debug)]" in out
