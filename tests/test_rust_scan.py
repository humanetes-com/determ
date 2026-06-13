"""Tests for pipeline_core.domain.rust_scan — regex-free Rust-ish text scanners.

Each test pins behavior against the regex the helper replaced (Task2 R7 migration).
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_scan import count_cfg_attrs, item_names

_OLD_SYMBOL_RE = re.compile(r'\b(?:fn|struct|enum|trait|type|const)\s+([A-Za-z_]\w*)')


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
