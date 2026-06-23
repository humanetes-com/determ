"""Tests for pipeline_core.domain.rust_scan — regex-free Rust-ish text scanners.

Each test pins behavior against the regex the helper replaced (Task2 R7 migration).
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_scan import (
    branch_guard_label,
    count_cfg_attrs,
    has_nonempty_test_fn,
    is_rust_item_boundary_line,
    is_structural_brace_line,
    item_names,
    mod_body,
    mod_decls,
    module_use_imports,
    rust_code_fences,
)

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


def test_rust_delimiters_balanced():
    from pipeline_core.domain.rust_scan import rust_delimiters_balanced
    assert rust_delimiters_balanced("fn f() { let v = vec![1,2]; }") is True
    assert rust_delimiters_balanced("") is True
    assert rust_delimiters_balanced("fn f() { (a }") is False          # mismatched
    assert rust_delimiters_balanced('let s = "{ unbalanced ( in string";') is True  # string opaque
    assert rust_delimiters_balanced("// ) ] } all in a comment\n{}") is True
    assert rust_delimiters_balanced("fn f() {") is False               # unclosed brace


# --- P7: source-LINE classification + scan_downstream_api source scans ---


def _old_boundary(s: str) -> bool:
    return bool(re.match(r'^(?:pub\s+|async\s+|unsafe\s+)*fn\s+', s)) or bool(
        re.match(r'^(?:impl|struct|enum|trait)\b', s))


def _old_guard_label(s: str) -> str | None:
    if re.match(r'^(?:\}\s*)?else\s+if\b', s) or re.match(r'^if\b', s):
        return s[:120]
    if re.match(r'^(?:\}\s*)?else\s*\{', s):
        return "else branch"
    if '=>' in s and re.search(r'=>\s*\{?\s*$', s):
        return f"match arm: {s[:100]}"
    if re.match(r'^while\b', s):
        return s[:120]
    return None


_GUARD_LINES = [
    "fn foo() {", "pub fn foo() {", "pub async unsafe fn x() {", "unsafe fn y() {",
    "impl Foo {", "struct S {", "enum E {", "trait T {", "impls Foo", "structural",
    "if x > 0 {", "} else if y {", "else if z {", "} else {", "else {",
    "Pattern => {", "Some(x) =>", "a => b => {", "x => { y }", "while cond {",
    "let z = 1;", "fnord()", "iffy", "whilezzz", "", "} else x", "=>{",
]


def test_is_rust_item_boundary_line_matches_regex():
    for s in _GUARD_LINES:
        assert is_rust_item_boundary_line(s) == _old_boundary(s), s


def test_branch_guard_label_matches_regex():
    for s in _GUARD_LINES:
        assert branch_guard_label(s) == _old_guard_label(s), s


_STRUCT_LINES = ["}", "};", "},", "} else {", "}  ;  ", "} else { ;", "}x", "  }",
                 "} else x", "}else{", "} , ", "else {", "", "}}"]


def test_is_structural_brace_line_matches_regex():
    pat = re.compile(r'^\}(\s*else\s*\{)?\s*[;,]?\s*$')
    for t in _STRUCT_LINES:
        assert is_structural_brace_line(t) == bool(pat.match(t)), t


_TEST_BODIES = [
    "#[test] fn t() { assert!(x); }", "#[test] fn t() {}", "#[test] fn t() {   }",
    "#[test]\nfn t() {\n  let x = 1;\n}", "#[test] fn t()", "no test here",
    "#[test] fn t() {", "#[test] fn a() {} #[test] fn b() { ok(); }", "#[test]{x",
]


def test_has_nonempty_test_fn_matches_regex():
    pat = re.compile(r'#\[test\][^{]*\{[^}]*\S', re.DOTALL)
    for b in _TEST_BODIES:
        assert has_nonempty_test_fn(b) == bool(pat.search(b)), b


def _old_uses(text, module):
    mod = re.escape(module)
    a = [m.group(1) for m in re.finditer(rf'use {mod}::\{{([^}}]+)\}}', text)]
    b = [(m.group(1), m.group(2)) for m in re.finditer(rf'use {mod}::(\w+)::\{{([^}}]+)\}}', text)]
    c = [(m.group(1), m.group(2)) for m in re.finditer(rf'use {mod}::(\w+)::(\w+)\b', text)]
    return a, b, c


_USE_TEXTS = [
    "use argh_shared::{A, B, C};", "use argh_shared::sub::{X, Y};",
    "use argh_shared::sub::Item;", "use argh_shared::{};",
    "use argh_shared::a::{P}; use argh_shared::Q;",
    "xuse argh_shared::{Z};", "use argh_shared::{A}; use argh_shared::b::C;",
    "use argh_shared::{a::b}; use argh_shared::s::Item}", "no imports", "",
]


def test_module_use_imports_matches_regex():
    for t in _USE_TEXTS:
        assert module_use_imports(t, "argh_shared") == _old_uses(t, "argh_shared"), t


_FENCE_TEXTS = [
    "```rust\nfn f() {}\n```", "text ```rust  code ``` more", "```rust no close",
    "```rust A ``` ```rust B ```", "```python x ```", "no fence", "",
    "```rust\n```", "```rustcode```",
]


def test_rust_code_fences_matches_regex():
    pat = re.compile(r'```rust\s*(.*?)```', re.DOTALL)
    for t in _FENCE_TEXTS:
        assert rust_code_fences(t) == [m.group(1) for m in pat.finditer(t)], t


# --- P7: more source line/token classifiers ---

from pipeline_core.domain.rust_scan import (  # noqa: E402
    contains_decl_keyword,
    has_no_std_attr,
    is_type_decl_line,
    starts_with_pub,
)


def test_is_type_decl_line_matches_regex():
    pat = re.compile(r'\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl|type|macro_rules)\b')
    cases = [
        "struct S {", "pub struct S {", "pub(crate) struct S {", "  impl Foo {",
        "pub(super) enum E {", "type Alias = u8;", "macro_rules! m {", "fn foo() {",
        "public struct X", "pubstruct X", "pub (crate) struct X", "let x = 1;",
        "trait T {}", "typedef", "pub(crate)struct X", "", "  ", "pub fn x()",
    ]
    for s in cases:
        assert is_type_decl_line(s) == bool(pat.match(s)), s


def test_starts_with_pub_matches_regex():
    pat = re.compile(r'\s*pub\b')
    for s in ["pub fn x", "  pub struct", "public", "pubfn", "pub", "fn pub", "", "pub(crate) x"]:
        assert starts_with_pub(s) == bool(pat.match(s)), s


def test_contains_decl_keyword_matches_regex():
    pat = re.compile(r"\b(mod|fn|struct|enum|trait|impl|use|extern|"
                     r"static|const|type|macro_rules|pub|unsafe)\b")
    cases = [
        "    fn foo() {", "let x = 1;", "mymod::thing()", "const_x = 2",
        "macro_rules! m", "  pub unsafe fn", "use crate::x;", "no keywords here",
        "extern crate", "typename", "a fn b", "", "#[cfg(test)]",
    ]
    for s in cases:
        assert contains_decl_keyword(s) == bool(pat.search(s)), s


def test_has_no_std_attr_matches_regex():
    pat = re.compile(r"#\s*!\s*\[\s*no_std\s*\]")
    cases = [
        "#![no_std]", "# ! [ no_std ]", "#![ no_std ]", "#[no_std]",
        "//#![no_std]\nfn x", "#![no_stdx]", "#![ nostd ]", "text #![no_std] more",
        "#!", "#![no_std", "", "#!\t[\nno_std\t]",
    ]
    for s in cases:
        assert has_no_std_attr(s) == bool(pat.search(s)), s


# --- P7: whole-word presence / count / macro-call helpers ---

from pipeline_core.domain.rust_scan import (  # noqa: E402
    contains_word,
    count_macro_calls,
    count_word,
)

_WORD_TEXTS = [
    "foo bar foo", "foobar foo", "xfoo foo_ foo", "Foo::foo()", "no match",
    "foo!foo", "a foo b foo c", "", "foo", "_foo foo9 foo",
]


def test_contains_word_matches_regex():
    for t in _WORD_TEXTS:
        for w in ("foo", "bar", "Foo", "x"):
            assert contains_word(t, w) == bool(re.search(r"\b" + re.escape(w) + r"\b", t)), (t, w)


def test_count_word_matches_regex():
    for t in _WORD_TEXTS:
        for w in ("foo", "bar", "Foo"):
            assert count_word(t, w) == len(re.findall(r"\b" + re.escape(w) + r"\b", t)), (t, w)


_MACRO_TEXTS = [
    "foo!()", "foo !()", "foo\t!()", "foobar!()", "xfoo!()", "foo() foo!()",
    "vec![1] foo!{}", "no macro", "", "foo ! foo!",
]


def test_count_macro_calls_matches_regex():
    for t in _MACRO_TEXTS:
        for w in ("foo", "vec"):
            assert count_macro_calls(t, w) == len(re.findall(r"\b" + re.escape(w) + r"\s*!", t)), (t, w)


# --- P7: Rust comment stripping ---

from pipeline_core.domain.rust_scan import (  # noqa: E402
    strip_comment_spans,
    strip_line_block_comments,
)

_COMMENT_TEXTS = [
    "let x = 1; // trailing\ny", "/* block */ code", "a /* b // c */ d",
    "/* // */ x", "# attr line\nfn f", "code /* no close", "// /* x */ still",
    "no comments", "", "/*/", "#[derive(Debug)]\nstruct S;", "a//b\n/*c*/d",
]


def test_strip_comment_spans_matches_regex():
    pat = re.compile(r'//[^\n]*|/\*.*?\*/|#[^\n]*', re.DOTALL)
    for t in _COMMENT_TEXTS:
        assert strip_comment_spans(t) == pat.sub("", t), t


def test_strip_line_block_comments_matches_regex():
    for t in _COMMENT_TEXTS:
        t1 = re.sub(r'//[^\n]*', '', t)
        expected = re.sub(r'/\*.*?\*/', '', t1, flags=re.DOTALL)
        assert strip_line_block_comments(t) == expected, t
