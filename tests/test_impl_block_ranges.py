"""Unit tests for the pure impl-block byte-range scanner
(`pipeline_core.domain.impl_block_ranges`) relocated from pipeline.py.

Pins the regex-free lexer: it must find every impl block's byte range while
ignoring `impl`-like text and braces inside comments, strings, raw strings,
char literals, and lifetimes.
"""
from __future__ import annotations

import re

from pipeline_core.domain.impl_block_ranges import (
    impl_where_clause,
    parse_impl_block_ranges,
)


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


def test_raw_string_with_hashes_braces_ignored():
    src = 'impl Foo {\n    fn a() { let s = r#"} { unbalanced "#; }\n}\n'
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1
    assert _slice(src, ranges[0]).endswith("}")


def test_string_escape_quote_ignored():
    src = 'impl Foo {\n    fn a() { let s = "a \\" } still in string"; }\n}\n'
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1


def test_unterminated_body_yields_no_range():
    # `{` opens but never closes → depth never returns to 0 → no range emitted.
    src = "impl Foo {\n    fn a() {\n"
    assert parse_impl_block_ranges(src) == []


def test_char_literal_with_escape():
    src = "impl Foo {\n    fn a() { let c = '\\}'; }\n}\n"
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 1


def test_multiple_impls_and_comment_with_impl_word():
    src = (
        "// impl NotReal {\n"
        "impl A {}\n"
        "/* impl AlsoNot */\n"
        "impl B {}\n"
    )
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 2
    heads = [src[r.header_start:r.body_open] for r in ranges]
    assert heads == ["impl A ", "impl B "]


# --- impl_where_clause (regex locator → structural; byte-equal to the old
#     regex on normal input, improving on nested generics + comment mentions) ---
def _orig_where(file_text: str, container: str) -> str:
    """The regex implementation impl_where_clause replaced — the oracle."""
    if not container or not container.startswith("impl"):
        return ""
    body = container[len("impl"):].lstrip()
    if not body:
        return ""
    nt = re.sub(r"\s+", " ", body).strip()
    ntext = re.sub(r"\s+", " ", file_text)
    pat = re.compile(r"impl\s*(?:<[^>]*>\s*)?" + re.escape(nt))
    m = pat.search(ntext)
    if m is None:
        return ""
    end = ntext.find("{", m.end())
    if end < 0:
        return ""
    header = ntext[m.start():end]
    wm = re.search(r"\bwhere\b(.*)$", header, re.DOTALL)
    return wm.group(1).strip().rstrip(",").strip() if wm else ""


_WHERE_CASES = [
    ("impl<T, E> Context<T, E> for Result<T, E>\nwhere\n    E: ext::StdError,\n{\n}",
     "impl Context<T, E> for Result<T, E>"),
    ("impl Display for Foo {\n}", "impl Display for Foo"),
    ("impl Foo for Bar where T: A {}", "impl Foo for Bar"),
    ("impl<T> Wrap<T> for Box<T> where T: Clone, T: Send {}", "impl Wrap<T> for Box<T>"),
    ("struct S;\nimpl S {}\nimpl Trait for S where X: Y {}", "impl Trait for S"),
    ("impl NoMatch for Z {}", "impl Other for Q"),
    ("impl  Spaces   for   Thing   where  A : B  {}", "impl Spaces for Thing"),
    ("not an impl", "impl Foo"),
    ("impl Foo {}", ""),
]


def test_impl_where_clause_matches_old_regex_on_normal_input():
    for ft, c in _WHERE_CASES:
        assert impl_where_clause(ft, c) == _orig_where(ft, c), (ft, c)


def test_impl_where_clause_handles_nested_own_generics():
    # The old `<[^>]*>` regex returned "" here (stopped at the first `>`);
    # balanced `<>` stripping recovers the clause.
    ft = "impl<T: Trait<X>> Wrap<T> for Box<T> where T: Clone {}"
    c = "impl Wrap<T> for Box<T>"
    assert impl_where_clause(ft, c) == "T: Clone"
    assert _orig_where(ft, c) == ""  # documents the bug the rewrite fixes


def test_impl_where_clause_ignores_commented_impl_mention():
    # The old regex matched the `impl … where …` text inside a comment; the
    # comment-aware scanner skips it and finds the real (clause-less) impl.
    ft = "// impl Ghost for X where SHOULD: NotMatch\nimpl Ghost for X {}"
    c = "impl Ghost for X"
    assert impl_where_clause(ft, c) == ""


# --- ratchet sweep 2026-07-04: lexer edges previously reached only through
# --- e2e flows (coverage floor pipeline_core.domain) ----------------------- #

def test_raw_strings_and_char_literals_skipped():
    src = (
        'impl Foo {\n'
        '    fn a(&self) -> &str { r#"raw } brace"# }\n'
        '    fn b(&self) -> char { \'}\' }\n'
        '    fn c(&self) -> char { \'\\n\' }\n'
        '    fn d(&self) -> &str { r"plain raw }" }\n'
        '}\n'
        'impl Bar {}\n'
    )
    ranges = parse_impl_block_ranges(src)
    assert len(ranges) == 2
    assert _slice(src, ranges[0]).rstrip().endswith("}")
    assert "Bar" in _slice(src, ranges[1])


def test_bare_r_identifier_not_raw_string():
    src = "impl Foo { fn a(&self) -> u8 { r } }\nimpl Baz {}\n"
    assert len(parse_impl_block_ranges(src)) == 2


def test_comments_inside_impl_header_skipped():
    src = ("impl /* header } comment */ Foo // trailing }\n"
           "{ fn a(&self) {} }\n")
    (r,) = parse_impl_block_ranges(src)
    assert "fn a" in src[r.header_start:r.body_close + 1]


def test_impl_where_clause_lookup():
    src = ("impl<T> Wrap<T> for Vec<T>\n"
           "where\n    T: Clone,\n"
           "{ fn w(&self) {} }\n")
    # container carries the impl's own generics stripped (extractor form)
    assert impl_where_clause(src, "impl Wrap<T> for Vec<T>") == "T: Clone"
    assert impl_where_clause(src, "impl Missing") == ""
    assert impl_where_clause(src, "not an impl") == ""
    assert impl_where_clause(src, "impl") == ""
