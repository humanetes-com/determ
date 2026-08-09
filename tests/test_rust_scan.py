"""Tests for pipeline_core.domain.rust_scan — regex-free Rust-ish text scanners.

Each test pins behavior against the regex the helper replaced (Task2 R7 migration).
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_scan import (
    attributed_fn_names,
    branch_guard_label,
    count_attrs,
    count_cfg_attrs,
    declared_fn_names,
    drop_duplicate_attributes,
    duplicate_attribute_runs,
    has_code_brace,
    has_nonempty_test_fn,
    is_rust_item_boundary_line,
    is_structural_brace_line,
    item_names,
    mod_body,
    mod_decls,
    module_use_imports,
    rust_code_fences,
    strip_outer_attributes,
    struct_names,
    top_level_item_names,
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


# --- source-LINE classification + scan_downstream_api source scans ---


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


# --- more source line/token classifiers ---

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


# --- whole-word presence / count / macro-call helpers ---

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


# --- Rust comment stripping ---

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


# --- attribute-opener scan ---

from pipeline_core.domain.rust_scan import attr_opener_end  # noqa: E402


def test_attr_opener_end_matches_regex():
    pat = re.compile(r'#\s*(!?)\s*\[')
    for t in ["#[derive]", "#![no_std]", "# ! [ doc ]", "#  [x]", "#!x", "not #",
              "#", "#[", "#![", "## [", "#!![", ""]:
        for i in range(len(t) + 1):
            m = pat.match(t, i)
            expected = (m.group(1) == '!', m.end()) if m else None
            assert attr_opener_end(t, i) == expected, (t, i)


# --- test-attribute line detection ---

from pipeline_core.domain.rust_scan import is_attr_line, is_test_attr_line  # noqa: E402


def test_is_attr_line_matches_regex():
    pat = re.compile(r'^\s*#\s*\[')
    for s in ["#[test]", "  # [ derive ]", "#![no_std]", "fn x", "#x", "# ", "", "#["]:
        assert is_attr_line(s) == bool(pat.match(s)), s


def test_is_test_attr_line_matches_regex():
    pat = re.compile(r'^\s*#\s*\[\s*(?:test|tokio::test|async_std::test|serial_test::serial|'
                     r'rstest|parameterized|test_case)\s*[\s\],(]')
    for s in ["#[test]", "#[ test ]", "#[tokio::test]", "#[rstest]", "#[test_case]",
              "#[testing]", "#[test_foo]", "  #[serial_test::serial]", "#[cfg(test)]",
              "#[test(", "#[parameterized]", "fn t", "", "#[test"]:
        assert is_test_attr_line(s) == bool(pat.search(s)), s


from pipeline_core.domain.rust_scan import replace_elision_placeholders  # noqa: E402


def test_replace_elision_placeholders_matches_regex():
    for t in ["fn f() { ... }", "{...}", "{  ...  }", "no placeholder", "{ .. }",
              "a { ... } b { ... }", "{ ... ", "", "{\n...\n}"]:
        assert replace_elision_placeholders(t) == re.sub(r'\{\s*\.\.\.\s*\}', '{ todo!() }', t), t


from pipeline_core.domain.rust_scan import line_has_assertion  # noqa: E402


def test_line_has_assertion_matches_regex():
    pat = re.compile(r'\b(assert(?:_eq|_ne|_matches|_approx_eq)?!|assert!\s*\(|\.unwrap\(\)|\.expect\()')
    cases = [
        "assert!(x)", "assert_eq!(a, b)", "let y = z.unwrap();", "v.expect(\"msg\")",
        "assert_ne!(1, 2)", "assert_matches!(x, Some(_))", ".unwrap()", ".expect(",
        "xassert!(", "myassert_eq!", "assert_eqx!()", "let v = vec![1];", "// assert!(x)",
        "assert! (y)", "a.unwrap()", "assert", "",
    ]
    for s in cases:
        assert line_has_assertion(s) == bool(pat.search(s)), s


from pipeline_core.domain.rust_scan import gen_mod_body_start  # noqa: E402


def test_gen_mod_body_start_matches_regex():
    pat = re.compile(r'(?:#\[[^\]]*\]\s*\n)*\s*(?:pub\s+)?mod\s+gen_\w+\s*\{')
    cases = [
        "mod gen_tests_1 { fn t() {} }", "pub mod gen_x {", "#[cfg(test)]\nmod gen_y {",
        "code\nmod gen_z {\n}", "no gen mod", "mod gen_ {", "mod genx {",
        "xmod gen_a {", "#[a]\n#[b]\npub mod gen_q   {", "",
    ]
    for t in cases:
        m = pat.search(t)
        assert gen_mod_body_start(t) == (m.end() if m else None), t


from pipeline_core.domain.rust_scan import named_fn_decl_start  # noqa: E402


def test_named_fn_decl_start_matches_regex():
    import random
    toks = ["pub ", "pub(crate) ", "async ", "unsafe ", "const ", "fn ", "foo", "bar",
            " ", "\t", "\n", "x", "fn foo", "(", ")"]
    for _ in range(3000):
        t = "".join(random.choice(toks) for _ in range(random.randint(0, 9)))
        for nm in ("foo", "bar", "f"):
            pat = re.compile(r"(?:^|\n)([ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
                             r"(?:unsafe\s+)?(?:const\s+)?fn\s+" + re.escape(nm) + r"\b", re.M)
            m = pat.search(t)
            assert named_fn_decl_start(t, nm) == (m.start() if m else None), (t, nm)


def test_struct_names_skip_comments_strings_and_fn_bodies():
    text = (
        "// struct Fake { a: u8 }\n"
        "/// struct DocFake;\n"
        'const S: &str = "struct InString";\n'
        "struct Real { x: u8 }\n"
        "fn f() { struct Local; let _ = S; }\n"
        "struct AfterFn;\n"
    )
    assert struct_names(text) == ["Real", "AfterFn"]


def test_struct_names_fn_pointer_field_does_not_swallow_siblings():
    assert struct_names(
        "struct A { f: fn(u32) }\nstruct B { x: u8 }\n") == ["A", "B"]


def test_struct_names_bodyless_trait_fn_and_impl_methods():
    text = (
        "trait T { fn g(&self); }\n"
        "impl X { fn a(&self) { let y = 1; } fn b(&self) {} }\n"
        "struct C;\n"
    )
    assert struct_names(text) == ["C"]


def test_strip_outer_attributes_removes_attrs_docs_and_comments():
    text = (
        '/// doc line\n'
        '// plain comment\n'
        '#[cfg(feature = "std")]\n'
        '  #[cold]\n'
        'pub fn f<T: Clone>(x: T) -> T {'
    )
    assert strip_outer_attributes(text) == "pub fn f<T: Clone>(x: T) -> T {"


def test_strip_outer_attributes_masks_brackets_inside_literals():
    text = '#[doc = "a ] b"]\n#[derive(Debug)]\nstruct S;'
    assert strip_outer_attributes(text) == "struct S;"


def test_strip_outer_attributes_leaves_a_bare_signature_and_unterminated_attr():
    assert strip_outer_attributes("fn g() -> u8") == "fn g() -> u8"
    unterminated = '#[cfg(feature = "std"\nfn g() -> u8'
    assert strip_outer_attributes(unterminated) == unterminated


def test_has_code_brace_ignores_strings_comments_and_generics():
    assert has_code_brace("fn f<T: Clone>(x: T) -> T") is False
    assert has_code_brace('fn f(s: &str) // { not code') is False
    assert has_code_brace('const S: &str = "{";') is False
    assert has_code_brace("fn f() where F: Fn() -> ()") is False
    assert has_code_brace("fn f() -> u8 { 1 }") is True


def test_top_level_item_names_is_depth_and_comment_aware():
    text = (
        "pub fn get() -> i32 { fn nested() -> i32 { 1 } nested() }\n"
        "impl Foo { fn method(&self) {} }\n"
        "// fn commented() {}\n"
        'const S: &str = "fn in_string()";\n'
        "struct Real;\n"
    )
    # `method` is inside the impl block (depth 1) — not a top-level item.
    assert top_level_item_names(text) == ["fn get", "const S", "struct Real"]


def test_top_level_item_names_detects_a_body_escaping_its_braces():
    before = "fn read() -> i32 { unimplemented!() }\n"
    after = "fn read() -> i32 { 1 }\nfn read() -> i32 { 2 }\n"
    assert top_level_item_names(before) == ["fn read"]
    assert top_level_item_names(after) == ["fn read", "fn read"]


from pipeline_core.domain.rust_scan import (  # noqa: E402
    body_is_only_redeclaration,
    redeclared_inner_body,
)


def test_body_is_only_redeclaration_catches_the_whole_item_answer():
    # The model answered with the whole fn when asked for a body; splicing it
    # between the real fn's braces parses and keeps the top-level item list
    # identical, so only this predicate rejects it.
    body = "unsafe fn drop_rest<C, E>(e: Own<Err>, t: TypeId)\nwhere\n    C: 'static,\n{\n    ptr::write(p, v);\n}"
    assert body_is_only_redeclaration(body, "drop_rest") is True
    assert top_level_item_names("fn drop_rest() {" + body + "}") == ["fn drop_rest"]


def test_body_is_only_redeclaration_ignores_attributes_and_trailing_comments():
    body = '#[inline]\n#[cfg(feature = "std")]\nfn get(&self) -> i32 {\n    1\n}\n// done\n'
    assert body_is_only_redeclaration(body, "get") is True


def test_body_is_only_redeclaration_allows_real_work():
    # A same-named nested helper that is actually CALLED is legal and works.
    assert body_is_only_redeclaration("fn go() -> i32 { 1 }\ngo()", "go") is False
    # A differently-named nested helper is the ordinary case.
    assert body_is_only_redeclaration("fn helper() -> i32 { 1 }", "go") is False
    # An ordinary body never matches.
    assert body_is_only_redeclaration("self.inner.fmt(f)", "fmt") is False
    assert body_is_only_redeclaration("", "fmt") is False
    assert body_is_only_redeclaration("fn fmt() {}", "") is False


def test_body_is_only_redeclaration_is_comment_and_string_aware():
    # A `}` inside a string must not be read as closing the nested item.
    body = 'fn render() -> String {\n    String::from("}")\n}'
    assert body_is_only_redeclaration(body, "render") is True
    # Code after the nested item's real close is real work.
    body2 = 'fn render() -> String {\n    String::from("}")\n}\nrender()'
    assert body_is_only_redeclaration(body2, "render") is False


def test_redeclared_inner_body_returns_the_work_the_model_wrote():
    # The wrapper is the mistake, not the contents — recovering the inner body
    # is what turns the refusal into a landed fill instead of a lost attempt.
    body = 'fn get(&self) -> i32 {\n        let x = 1;\n        x + 1\n}'
    assert redeclared_inner_body(body, "get") == "let x = 1;\nx + 1"
    # Detection and repair are the same question: True <=> not None.
    assert body_is_only_redeclaration(body, "get") is True


def test_redeclared_inner_body_distinguishes_no_match_from_nothing_to_recover():
    # An ordinary body has no wrapper at all.
    assert redeclared_inner_body("self.inner.fmt(f)", "fmt") is None
    # The shape matched but the nested fn is empty: still illegal (the outer fn
    # does nothing), yet there is no work to splice back.
    assert redeclared_inner_body("fn noop() {}", "noop") == ""
    assert body_is_only_redeclaration("fn noop() {}", "noop") is True


from pipeline_core.domain.rust_scan import (  # noqa: E402
    rust_delimiters_balanced,
)


# --- rust_delimiters_balanced ----------------------------------------------
# The gate that rejects corrupted LLM output before a gen_tests sibling is
# committed. Untested until now: every false NEGATIVE lets a broken sibling
# reach the crate (the whole module then fails to parse), and every false
# POSITIVE silently discards a good test block.
def test_balanced_accepts_well_formed_and_empty() -> None:
    assert rust_delimiters_balanced("")
    assert rust_delimiters_balanced("fn a() { let v = [1, 2]; }")


def test_balanced_rejects_the_common_corruptions() -> None:
    assert not rust_delimiters_balanced("fn a() { let v = [1, 2]; ")
    assert not rust_delimiters_balanced("fn a() { })")   # extra closer
    assert not rust_delimiters_balanced("fn a(] {}")     # crossed pair


def test_balanced_ignores_delimiters_inside_string_literals() -> None:
    assert rust_delimiters_balanced('let s = "unclosed { [ (";')
    assert rust_delimiters_balanced(r'let s = "esc \" still in { str";')


def test_balanced_ignores_delimiters_inside_char_literals() -> None:
    assert rust_delimiters_balanced("let c = '{';")
    assert rust_delimiters_balanced(r"let c = '\'';")


def test_balanced_ignores_delimiters_inside_comments() -> None:
    assert rust_delimiters_balanced("fn a() {} // trailing ) } ]")
    assert rust_delimiters_balanced("/* { [ ( */ fn a() {}")
    # Rust block comments nest — an inner /* must not end the outer one early.
    assert rust_delimiters_balanced("/* outer /* inner { */ still */ fn a() {}")


def test_balanced_counts_code_after_a_comment_closes() -> None:
    assert not rust_delimiters_balanced("// note\nfn a() {")


def test_balanced_treats_lifetimes_as_carrying_no_delimiter() -> None:
    """A lifetime is not a char literal: `'a` must not open a literal that
    swallows the rest of the file (which would report every later brace lost)."""
    assert rust_delimiters_balanced("fn a<'a>(x: &'a str) -> &'a str { x }")
    assert not rust_delimiters_balanced("fn a<'a>(x: &'a str) -> &'a str {")


def test_balanced_handles_a_quote_char_literal() -> None:
    assert rust_delimiters_balanced(r"""let q = '"'; let b = '{'; fn a() {}""")


def test_attributed_fn_names_counts_fns_not_attribute_lines() -> None:
    # The live divergence on `.skeleton-filled/stable/src/error.rs`: 30
    # `#[cold]` LINES against 16 annotated fns, because 17 attribute runs hold
    # a repeat (several x3). The standing handover invariant "#[cold] = 30,
    # must not grow" was counting the duplication, so it read GREEN on a tree
    # where real #[cold] fns were lost and RED on one that was merely cleaned.
    src = (
        '#[cfg(feature = "std")]\n'
        "#[cold]\n"
        '#[cfg(feature = "std")]\n'
        "#[cold]\n"
        "#[cold]\n"
        "pub fn triplicated(x: u8) -> u8 { x }\n"
        "\n"
        "#[cold]\n"
        "fn plain() {}\n"
        "\n"
        "#[coldish]\n"                  # a different attribute, not a prefix match
        "fn other() {}\n"
    )
    assert count_attrs(src, "cold") == 4        # occurrences
    assert attributed_fn_names(src, "cold") == ["triplicated", "plain"]


def test_duplicate_attribute_runs_finds_a_re_prepended_splice() -> None:
    # rustc accepts repeated cfg/cold, so this never fails a build — the only
    # symptom is a count nobody re-measures. The 2026-07-27 splice repair
    # stopped the accumulation; this is what keeps it stopped.
    src = (
        '#[cfg(any(feature = "std"))]\n'
        "#[cold]\n"
        '#[cfg(any(feature = "std"))]\n'
        "#[cold]\n"
        "fn f() {}\n"
        "\n"
        "#[inline]\n"                    # a clean run reports nothing
        "#[must_use]\n"
        "fn g() -> u8 { 0 }\n"
    )
    dups = duplicate_attribute_runs(src)
    assert [ln for ln, _ in dups] == [3, 4]
    assert {a for _, a in dups} == {'#[cfg(any(feature = "std"))]', "#[cold]"}
    assert duplicate_attribute_runs("#[inline]\nfn g() {}\n") == []


def test_drop_duplicate_attributes_keeps_the_first_and_its_indentation() -> None:
    src = (
        '    #[cfg(any(feature = "std"))]\n'
        "    #[cold]\n"
        '#[cfg(any(feature = "std"))]\n'
        "#[cold]\n"
        "    pub fn f() {}\n"
    )
    assert drop_duplicate_attributes(src) == (
        '    #[cfg(any(feature = "std"))]\n'
        "    #[cold]\n"
        "    pub fn f() {}\n"
    )
    assert duplicate_attribute_runs(drop_duplicate_attributes(src)) == []


def test_drop_duplicate_attributes_removes_a_wrapped_copy_whole() -> None:
    wrapped = (
        "#[allow(\n"
        "    clippy::needless_lifetimes,\n"
        "    clippy::wrong_self_convention,\n"
        ")]\n"
    )
    item = "pub fn provide(&self) -> u8 { 0 }\n"
    assert drop_duplicate_attributes(wrapped + wrapped + item) == wrapped + item


def test_drop_duplicate_attributes_leaves_a_clean_file_byte_identical() -> None:
    src = "#[inline]\n#[must_use]\nfn g() -> u8 { 0 }\n"
    assert drop_duplicate_attributes(src) is src


def test_drop_duplicate_attributes_does_not_merge_distinct_attributes() -> None:
    """Only byte-identical attributes are residue. Two cfgs that differ are two
    different gates, and dropping either would change what compiles."""
    src = (
        '#[cfg(feature = "std")]\n'
        '#[cfg(not(anyhow_no_core_error))]\n'
        "fn f() {}\n"
    )
    assert drop_duplicate_attributes(src) == src


def test_trait_impl_blocks_identity_is_the_whole_header() -> None:
    """Distinct impls must stay distinct. Every pair below lives in `anyhow`'s
    own `error.rs`/`context.rs` and compiles; an identity that reduced them to
    trait-name-and-self-type would report nine duplicates in two files, and a
    splice acting on that would delete working code."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = (
        "impl AsRef<dyn StdError + Send + Sync> for Error {}\n"
        "impl AsRef<dyn StdError> for Error {}\n"
        "impl From<Error> for Box<dyn StdError + Send + Sync + 'static> {}\n"
        "impl From<Error> for Box<dyn StdError + Send + 'static> {}\n"
        '#[cfg(feature = "std")]\n'
        "impl<C, E> StdError for ContextError<C, E> {}\n"
        '#[cfg(not(feature = "std"))]\n'
        "impl<C, E> StdError for ContextError<C, E> {}\n"
    )
    headers = [b.header for b in trait_impl_blocks(src)]
    assert len(headers) == 6
    assert len(set(headers)) == 6, "distinct impls collapsed onto one identity"


def test_trait_impl_blocks_identity_ignores_only_whitespace() -> None:
    """The same impl re-wrapped by a formatter is still the same impl."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    a = trait_impl_blocks("unsafe impl Send for Error {}\n")
    b = trait_impl_blocks("unsafe   impl\n    Send for\n    Error {}\n")
    assert [x.header for x in a] == [x.header for x in b]


def test_trait_impl_blocks_identity_keeps_the_cfg_string_literal() -> None:
    """Two impls gated onto different features both compile, and the only text
    telling them apart is inside the string literal. Blanking quoted bytes —
    which is what `code_mask` does, comments and strings together — merges
    them, and a splice acting on that deletes one of two working impls."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = (
        '#[cfg(feature = "std")]\n'
        "impl Debug for Error {}\n"
        '#[cfg(feature = "alloc")]\n'
        "impl Debug for Error {}\n"
    )
    assert len({b.header for b in trait_impl_blocks(src)}) == 2


def test_a_for_inside_a_doc_comment_is_not_a_trait_impl() -> None:
    """Scanning is comment-blind even though the identity is not: a `for` in a
    doc comment must not turn an inherent impl into a trait impl."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = "impl /* Display for Error */ Error {\n    fn a() {}\n}\n"
    assert trait_impl_blocks(src) == []


def test_trait_impl_blocks_body_does_not_change_the_identity() -> None:
    """Two impls with the same header are E0119 whatever their bodies say, so a
    re-authored body must not read as a different impl."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    one = trait_impl_blocks("impl Debug for Error { fn fmt() { a() } }")
    two = trait_impl_blocks("impl Debug for Error { fn fmt() { b() } }")
    assert one[0].header == two[0].header


def test_trait_impl_blocks_skips_inherent_impls_and_nested_ones() -> None:
    """A type may carry any number of inherent impls, so a second one is not a
    duplicate; and an impl inside another item's braces is not depth 0."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = (
        "impl Error {\n    fn a(&self) {}\n}\n"
        "impl Error {\n    fn b(&self) {}\n}\n"
        "mod inner {\n    impl Debug for Error {}\n}\n"
        "impl Debug for Error {}\n"
    )
    blocks = trait_impl_blocks(src)
    assert [b.header for b in blocks] == ["impl Debug for Error"]


def test_trait_impl_blocks_spans_attributes_and_a_braced_body() -> None:
    """The span has to start above the attributes: a deletion that leaves a
    stranded `#[cfg(...)]` gates whatever item follows it."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = (
        "struct Error;\n"
        '#[cfg(feature = "std")]\n'
        "unsafe impl Sync for Error {\n"
        "    // }  not a closer\n"
        "}\n"
        "struct After;\n"
    )
    blocks = trait_impl_blocks(src)
    assert len(blocks) == 1
    sliced = src[blocks[0].start:blocks[0].end]
    assert sliced.startswith('#[cfg(feature = "std")]')
    assert sliced.endswith("}")
    assert "struct After" not in sliced


def test_trait_impl_blocks_reads_past_a_fn_bound_arrow() -> None:
    """`->` inside the generic list is not a closing angle bracket. Counting it
    as one ends the parameter list early, and the `for` that makes this a trait
    impl is then looked for in the wrong text."""
    from pipeline_core.domain.rust_scan import trait_impl_blocks
    src = "impl<F: Fn(u8) -> u8> Handler for Wrap<F> {}\n"
    assert len(trait_impl_blocks(src)) == 1


def test_declared_members_reads_struct_fields_through_qualifiers() -> None:
    """Visibility and a path-qualified field type must not become members.
    `crate` is followed by `::`, which is a path separator and not a field's
    type annotation; reading it as one invents a member that every later
    definition then appears to drop."""
    from pipeline_core.domain.rust_scan import declared_members
    src = (
        "#[repr(C)]\n"
        "pub(crate) struct ErrorImpl<E = ()> {\n"
        "    pub(crate) vtable: &'static crate::error::ErrorVTable,\n"
        "    backtrace: crate::backtrace::Backtrace,\n"
        "    _error: E,\n"
        "}"
    )
    assert declared_members(src) == frozenset({"vtable", "backtrace", "_error"})


def test_declared_members_skips_a_named_fn_pointer_parameter() -> None:
    """A vtable field may be written `unsafe fn(e: Own<ErrorImpl>)`. `e` is a
    parameter of the field's TYPE, not a second member — collecting it invents a
    member for the next authored definition to drop, and the caller acts on
    exactly that difference."""
    from pipeline_core.domain.rust_scan import declared_members
    src = (
        "struct ErrorVTable {\n"
        "    object_drop: unsafe fn(e: Own<ErrorImpl>),\n"
        "    object_ref: unsafe fn(r: Ref<ErrorImpl>) -> Ref<dyn StdError>,\n"
        "}"
    )
    assert declared_members(src) == frozenset({"object_drop", "object_ref"})


def test_declared_members_covers_enum_variants_and_their_fields() -> None:
    from pipeline_core.domain.rust_scan import declared_members
    src = (
        "pub(crate) enum ChainState<'a> {\n"
        "    Linked { next: Option<&'a (dyn StdError + 'static)> },\n"
        '    #[cfg(feature = "std")]\n'
        "    Buffered { rest: vec::IntoIter<&'a (dyn StdError + 'static)> },\n"
        "}"
    )
    assert declared_members(src) == frozenset(
        {"Linked", "next", "Buffered", "rest"})


def test_declared_members_is_empty_for_a_tuple_or_unit_shape() -> None:
    """Neither shape names anything, so neither can lose a name."""
    from pipeline_core.domain.rust_scan import declared_members
    assert declared_members("pub struct Error(NonNull<ErrorImpl>);") == frozenset()
    assert declared_members("struct Quoted<C>(C);") == frozenset()
    assert declared_members("pub struct Marker;") == frozenset()


def test_field_reads_does_not_count_a_method_of_the_same_name() -> None:
    """`anyhow` has both a public `Error::backtrace()` method and a private
    `ErrorImpl.backtrace` field. Counting the call would report the field as
    load-bearing on evidence that says nothing about the layout — measured on
    the 93-passing tree, 2 calls against 5 real field reads."""
    from pipeline_core.domain.rust_scan import field_reads
    src = (
        "let a = (*this.as_ptr()).backtrace;\n"
        "let b = err.backtrace();\n"
        "let c = other . backtrace ();\n"
        "let d = self.backtrace;\n"
    )
    assert field_reads(src, ["backtrace"]) == {"backtrace": 2}


def test_field_reads_reports_a_name_it_never_found_as_zero() -> None:
    """Zero and absent are different answers: a caller has to be able to tell
    "looked and found none" from "never asked"."""
    from pipeline_core.domain.rust_scan import field_reads
    assert field_reads("let x = a.kept;", ["kept", "gone"]) == {
        "kept": 1, "gone": 0}


def test_field_reads_ignores_a_dot_that_is_not_a_field_access() -> None:
    from pipeline_core.domain.rust_scan import field_reads
    src = "let r = ..vtable;\nlet s = \"x.vtable\";\n// y.vtable\nlet t = 1.5;\n"
    assert field_reads(src, ["vtable"]) == {"vtable": 0}


def test_declared_member_count_counts_a_tuple_shape_as_storage() -> None:
    """Rewriting `struct Error { ptr: Own<ErrorImpl> }` as `struct
    Error(NonNull<ErrorImpl>)` loses the NAME 34 live bodies read and keeps the
    storage — those bodies can say `.0`. A count that saw only named members
    would read the re-spelling as a type emptied to nothing."""
    from pipeline_core.domain.rust_scan import declared_member_count
    assert declared_member_count("struct Error { ptr: Own<ErrorImpl> }") == 1
    assert declared_member_count("pub struct Error(NonNull<ErrorImpl>);") == 1
    assert declared_member_count("struct Pair(u8, Vec<(u8, u8)>);") == 2
    assert declared_member_count("struct Marker;") == 0


def test_declared_member_count_reads_past_a_generic_default() -> None:
    """`struct Foo<E = ()>(E);` has two parenthesised groups and only the last
    one is storage."""
    from pipeline_core.domain.rust_scan import declared_member_count
    assert declared_member_count("struct Foo<E = ()>(E);") == 1


def test_declared_member_count_reads_only_the_first_item_of_an_entry() -> None:
    """A stored definition is not always one item. The crate-wide layout
    registry holds `Error` as a struct plus `unsafe impl Send` plus `unsafe impl
    Sync` under one key; a scan that runs to the end of the blob finds the last
    impl's `}` and reports the tuple struct as having no storage at all."""
    from pipeline_core.domain.rust_scan import (
        declared_member_count,
        declared_members,
    )
    entry = (
        "#[repr(transparent)]\n"
        "pub struct Error(core::ptr::NonNull<crate::error::ErrorImpl>);\n"
        "unsafe impl Send for Error {}\n"
        "unsafe impl Sync for Error {}"
    )
    assert declared_member_count(entry) == 1
    assert declared_members(entry) == frozenset()


def test_declared_fn_names_separates_the_field_and_fn_namespaces() -> None:
    # Rust lets a struct field and a free fn share a name, and a note may name a
    # real field while prescribing a call that cannot resolve. `anyhow`'s
    # ErrorVTable declares an `object_drop_rest` FIELD whose value is the fn
    # `object_drop_front`; no fn `object_drop_rest` exists in the crate.
    text = """
struct ErrorVTable {
    object_drop: unsafe fn(Own<ErrorImpl>),
    object_drop_rest: unsafe fn(Own<ErrorImpl>, TypeId),
}
unsafe fn object_drop_front(e: Own<ErrorImpl>, t: TypeId) {}
const object_drop_const: u8 = 0;
"""
    names = declared_fn_names(text)
    assert "object_drop_front" in names
    assert "object_drop_rest" not in names
    # A const or struct of the same name is not something callable by it.
    assert "object_drop_const" not in names
    assert "ErrorVTable" not in names


def test_declared_fn_names_finds_nested_and_modified_fns() -> None:
    text = "impl X { pub async unsafe fn inner<T>(&self) -> T { fn helper() {} } }"
    assert declared_fn_names(text) == frozenset({"inner", "helper"})
