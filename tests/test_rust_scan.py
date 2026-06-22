"""Tests for pipeline_core.domain.rust_scan — regex-free Rust-ish text scanners.

Each test pins behavior against the regex the helper replaced (Task2 R7 migration).
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_scan import count_cfg_attrs, item_names, mod_body, mod_decls

_OLD_SYMBOL_RE = re.compile(r'\b(?:fn|struct|enum|trait|type|const)\s+([A-Za-z_]\w*)')
_OLD_MOD_RE = re.compile(r'\bmod\s+(\w+)\s*;')


def test_mod_decls_matches_old_regex():
    samples = [
        "mod foo;",
        "pub mod bar;",
        "mod  spaced ;\nmod two;",
        "mod inline { fn x() {} }",          # inline module → no decl
        "mod a;\nmod b { }\nmod c ;",
        "pub(crate) mod nested;",
        "let xmod = 1; modfoo();",            # not the keyword token
        "",
        "mod foo;bar baz",                    # `;` mid-token after name
    ]
    for s in samples:
        assert mod_decls(s) == _OLD_MOD_RE.findall(s), s


def test_mod_decls_excludes_inline_modules():
    assert mod_decls("mod a;\nmod b {\n    fn f() {}\n}\nmod c;") == ["a", "c"]


def test_item_names_basic_items():
    text = "pub fn foo() {}\nstruct Bar<T> { x: u8 }\nenum E { A }\ntrait T {}\ntype Alias = u8;\nconst K: u8 = 1;"
    assert item_names(text) == ["foo", "Bar", "E", "T", "Alias", "K"]


def test_item_names_matches_old_regex():
    samples = [
        "pub fn foo(a: i32) -> i32 { unimplemented!() }",
        "  struct Bar<T>;\n  pub(crate) enum Baz { A, B }",
        "type R = Result<(), E>;\nconst MAX: usize = 9;",
        "fn a(){} fn b(){} trait Tr: Sized {}",
        "",  # empty
        "no items here, just prose with the word function",
    ]
    for s in samples:
        assert item_names(s) == _OLD_SYMBOL_RE.findall(s), s


def test_item_names_ignores_keyword_substrings():
    # "myfn" / "const_x" are not the keyword token → no match (whitespace-delimited).
    assert item_names("let myfn = 1; let const_x = 2;") == []


def test_count_cfg_attrs_counts_matching_predicates():
    text = (
        '#[cfg(feature = "error_generic_member_access")]\nfn a() {}\n'
        '#[cfg(feature = "other")]\nfn b() {}\n'
        '#[cfg(anyhow_no_core_error)]\nfn c() {}\n'
    )
    assert count_cfg_attrs(text, ("error_generic_member_access", "anyhow_no_core_error")) == 2


def test_count_cfg_attrs_matches_old_regex():
    needles = ("error_generic_member_access", "anyhow_no_core_error")
    old_re = re.compile(r"#\[cfg\([^)]*(error_generic_member_access|anyhow_no_core_error)")
    samples = [
        '#[cfg(error_generic_member_access)] #[cfg(anyhow_no_core_error)]',
        '#[cfg(all(feature = "x"))] no needle here',
        '',
        '#[cfg(feature = "anyhow_no_core_error")] twice #[cfg(anyhow_no_core_error)]',
    ]
    for s in samples:
        assert count_cfg_attrs(s, needles) == len(old_re.findall(s)), s


def test_count_cfg_attrs_no_needles():
    assert count_cfg_attrs("#[cfg(feature = \"x\")]", ()) == 0


_PRELUDE_CASES = [
    (("std::", "alloc::"), "String", r'use (?:std|alloc)::[^;]*\bString\b'),
    (("std::", "alloc::"), "Vec", r'use (?:std|alloc)::[^;]*\bVec\b'),
    (("std::error::Error",), "Error", r'use std::error::Error'),
    (("std::fmt", "core::fmt"), "fmt", r'use (?:std|core)::fmt'),
]


def test_use_imports_symbol_matches_old_regex():
    from pipeline_core.domain.rust_scan import use_imports_symbol
    blocks = [
        "use super::*;\nuse std::string::String;\n",
        "use alloc::vec::Vec;\nuse core::fmt::Display;",
        "let s: MyString = x;",            # MyString is not a `String` word match
        "use std::error::Error as E;",
        "fn t() { let v: Vec<u8> = vec![]; }",  # Vec used but not imported
        "reuse std::vec::Vec;",            # `use ` found inside `reuse ` (regex parity)
    ]
    for prefixes, symbol, pat in _PRELUDE_CASES:
        old = re.compile(pat)
        for b in blocks:
            assert use_imports_symbol(b, prefixes, symbol) == bool(old.search(b)), (b, symbol)


def test_find_mod_opener_and_after_brace_newline():
    from pipeline_core.domain.rust_scan import after_brace_newline, find_mod_opener
    # gen-only finds the gen_ module; insert-before index is the `pub`/`mod` start.
    block = "fn f(){}\npub mod gen_tests_1 {\n    body\n}"
    s, b = find_mod_opener(block, gen_only=True)
    assert block[s:s + 3] == "pub" and block[b] == "{"
    assert find_mod_opener("mod helper {}", gen_only=True) is None      # not gen_
    assert find_mod_opener("model gen_x {}", gen_only=False) is None    # `mod` not a keyword
    # after-brace newline points just past the `{ … \n`.
    ins = after_brace_newline(block, b)
    assert block[:ins].endswith("{\n")
    assert after_brace_newline("mod gen_x { no nl }", 9) is None


def test_count_test_attrs_matches_old_regex():
    from pipeline_core.domain.rust_scan import count_test_attrs
    old = lambda b: len(re.findall(r'#\[(?:\w+::)*test\]', b))  # noqa: E731
    for b in ["#[test]", "#[tokio::test]\n#[test]", "#[a::b::test] #[cfg(test)]",
              "#[test_helper]", "no attrs", "#[test] #[test]"]:
        assert count_test_attrs(b) == old(b), b


def test_mod_body_extracts_dedented_body():
    block = "mod gen_tests_1 {\n    use super::*;\n    #[test]\n    fn t() {}\n}"
    assert mod_body(block) == "use super::*;\n#[test]\nfn t() {}\n"


def test_mod_body_nested_braces_and_absent():
    block = "pub mod gen_x {\n    fn f() { if true { g(); } }\n}"
    assert mod_body(block) == "fn f() { if true { g(); } }\n"
    assert mod_body("fn bare() {}") is None      # no `mod NAME {` opener
