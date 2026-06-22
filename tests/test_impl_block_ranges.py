"""Unit tests for the pure impl-block byte-range scanner
(`pipeline_core.domain.impl_block_ranges`) relocated from pipeline.py in P6.

Pins the regex-free lexer: it must find every impl block's byte range while
ignoring `impl`-like text and braces inside comments, strings, raw strings,
char literals, and lifetimes.
"""
from __future__ import annotations

from pipeline_core.domain.impl_block_ranges import parse_impl_block_ranges


def _slice(src: str, r) -> str:
    return src[r.header_start:r.body_close + 1]


def test_single_impl_block():
    src = "impl Foo {\n    fn a() {}\n}\n"
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1
    assert src[ranges[0].header_start:ranges[0].body_open] == "impl Foo "
    assert _slice(src, ranges[0]) == "impl Foo {\n    fn a() {}\n}"


def test_two_impls_distinct_where_clauses():
    src = (
        "impl<T> Mut<T> where T: Sized {\n    fn a() {}\n}\n"
        "impl<T> Mut<T> {\n    fn b() {}\n}\n"
    )
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 2
    h0 = src[ranges[0].header_start:ranges[0].body_open]
    h1 = src[ranges[1].header_start:ranges[1].body_open]
    assert "where T: Sized" in h0
    assert "where" not in h1


def test_braces_in_string_and_comment_ignored():
    src = (
        'impl Foo {\n'
        '    fn a() {\n'
        '        let s = "} not a close {";\n'
        '        // } also not a close\n'
        '        /* nested /* } */ still inside */\n'
        '    }\n'
        '}\n'
    )
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1
    # close brace is the final one, not the one inside the string/comment.
    assert _slice(src, ranges[0]).endswith("}")
    assert _slice(src, ranges[0]).count("fn a()") == 1


def test_impl_keyword_inside_identifier_not_matched():
    src = "fn implementation() {}\nstruct Implicit;\n"
    assert parse_impl_block_ranges(src) == []


def test_lifetime_apostrophe_not_treated_as_char():
    src = "impl<'a> Foo<'a> {\n    fn a(&'a self) {}\n}\n"
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1
    assert "'a" in src[ranges[0].header_start:ranges[0].body_open]


def test_impl_semicolon_aborted():
    # `impl X;` is not valid Rust → no body, no range.
    src = "impl X;\nimpl Y {}\n"
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1
    assert src[ranges[0].header_start:ranges[0].body_open] == "impl Y "
