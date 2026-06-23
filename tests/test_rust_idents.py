"""Unit tests for `pipeline_core.domain.rust_idents.fn_reference_counts` (P7).

Pins byte-equivalence to the original regex pair it replaced
(`\\bfn\\s+([A-Za-z_]\\w*)` + per-name `\\b<name>\\b`) — the reference-frequency
map that drives leaf-first fill ordering must be unchanged.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_idents import (
    attr_call_args,
    camelcase_type_names,
    contains_fn_decl,
    crate_lint_attr_args,
    declares_fn,
    defined_type_names,
    defines_type,
    first_fn_decl_name,
    fn_decl_names,
    fn_reference_counts,
    identifier_tokens,
    impl_self_type,
    is_fn_signature_line,
    is_unsafe_fn,
    mask_identifiers,
    mixed_case_identifiers,
    use_symbol_paths,
)


def _old(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in re.finditer(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", text):
        name = m.group(1)
        if name in counts:
            continue
        counts[name] = len(re.findall(r"\b" + re.escape(name) + r"\b", text))
    return counts


CASES = [
    "fn foo() { foo(); foo(); }\nfn bar() { foo(); }",
    "pub fn a() {}\nunsafe fn b() { a(); b(); a(); }",
    "let p: fn(i32) -> i32 = id;\nfn id(x:i32)->i32{x}",  # fn-pointer not a name
    "fn/*c*/weird() {}",                                   # no ws after fn → no match
    "// fn commented() {}\nfn real() { real(); }",         # comment refs counted (approx)
    "fn  spaced () { spaced(); }",
    "struct fnnotword;\nfn x(){}",                         # 'fnnotword' != 'fn'
    "",
    "fn a(){}\nfn a(){}",                                   # dup name → first decl only
    "3fn nope() {}",                                        # '3fn' is one word
]


def test_byte_equivalent_to_original_regex():
    for text in CASES:
        assert fn_reference_counts(text) == _old(text), text


def test_first_declaration_order_preserved():
    out = fn_reference_counts("fn zeta(){ alpha(); }\nfn alpha(){ zeta(); zeta(); }")
    assert list(out.keys()) == ["zeta", "alpha"]
    assert out == {"zeta": 3, "alpha": 2}


_TYPE_CASES = [
    "struct Foo { x: Bar }\nimpl Foo for Baz {}\nfn f(){ let y: Own = Mut::new(); }",
    "impl<T> Wrapper<T> {}\ntrait Show {}\ntype Alias = Vec<u8>;",
    "Foo_Bar fooBar BAZ X Y9 3D Ωmega Café",            # boundary / non-ascii edges
    "impl(weird Notmatched\nimpl /*c*/ Skip\nimpl Real {}",  # \s+ gap requirement
    "type=Bad\ntype Good = u8;",
    "",
]


def test_camelcase_type_names_matches_regex():
    for t in _TYPE_CASES:
        assert camelcase_type_names(t) == set(re.findall(r"\b([A-Z][A-Za-z0-9]+)\b", t)), t


def test_defined_type_names_matches_regex():
    pat = re.compile(r"\b(?:struct|enum|trait|type|impl)\s+([A-Z][A-Za-z0-9]+)\b")
    for t in _TYPE_CASES:
        assert defined_type_names(t) == {m.group(1) for m in pat.finditer(t)}, t


_UNSAFE_CASES = [
    "unsafe fn foo()", "pub unsafe fn x()", "unsafe   fn y()",
    "unsafe extern fn z()", "unsafe/*c*/fn w()", "fn safe()",
    "myunsafe fn q()", "unsafefn r()", "async unsafe fn a()", "",
]


def test_is_unsafe_fn_matches_regex():
    for s in _UNSAFE_CASES:
        assert is_unsafe_fn(s) == bool(re.search(r"\bunsafe\s+fn\b", s)), s


_DECL_CASES = [
    ("fn foo(x:i32)", "foo"), ("pub fn foo()", "foo"), ("fn foobar()", "foo"),
    ("fn  foo ()", "foo"), ("fn baz()", "foo"), ("fn/*c*/foo()", "foo"),
    ("impl X { fn foo() {} }", "foo"), ("fn a()", "a"),
]


def test_declares_fn_matches_regex():
    for s, name in _DECL_CASES:
        expected = bool(re.search(r"\bfn\s+" + re.escape(name) + r"\b", s))
        assert declares_fn(s, name) == expected, (s, name)


# --- P7 step 1: source-regex→structured migrations (byte-equivalence pins) ---

_FN_DECL_CASES = [
    "fn foo() {}", "pub fn bar<T>() {}", "fn  baz ()", "fn(i32)->i32", "fn /*c*/ x()",
    "fn 9z()", "let f: fn() = g;", "struct Foo; fn a(){} fn b<T:Clone>(){}",
    "// fn c()", "fn", "fnord x", "myfn y()", "fn _under()", "fn T9<X>()", "fn  (x)", "",
]


def test_contains_fn_decl_matches_regex():
    for t in _FN_DECL_CASES:
        assert contains_fn_decl(t) == bool(re.search(r"\bfn\s+\w+", t)), t


def test_fn_decl_names_matches_regex():
    pat = re.compile(r"\bfn\s+([A-Za-z_]\w*)\s*[<(]")
    for t in _FN_DECL_CASES:
        assert fn_decl_names(t) == {m.group(1) for m in pat.finditer(t)}, t


def test_first_fn_decl_name_matches_regex():
    pat = re.compile(r"\bfn\s+([A-Za-z_]\w*)")
    for t in _FN_DECL_CASES:
        m = pat.search(t)
        assert first_fn_decl_name(t) == (m.group(1) if m else None), t


_USE_CASES = [
    "use std::fmt;", "use foo::bar::{A, B, C};", "use a::b::Baz;", "use x;",
    "use ::root::X;", "use  spaced :: thing ;", "use foo::{};", "use foo::{a};",
    "used::nope; use real::X;", "use a::b::{C, D}; use e::F;", "use crate::{x::Y, z};",
    "use\tw::Q;", "pub use re::X;", "use a::b::{c, use_d::E};", "usefoo::X;",
    "use foo;", "use a::{b::{c}};", "use use b;", "",
]


def test_use_symbol_paths_matches_regex():
    pat = re.compile(r"\buse\s+([\w:]+(?:::\{[^}]+\})?)\s*;")
    for t in _USE_CASES:
        assert use_symbol_paths(t) == [m.group(1) for m in pat.finditer(t)], t


_ATTR_CASES = [
    "#[derive(Debug)]", "#[derive(Debug, Clone, PartialEq)]", "#[derive()]", "#[derive(",
    "#[derive(A)] #[derive(B,C)]", "foo #[derive(X)] bar", "#[serde(rename = \"x\")]",
    "#[derive(Debug)]#[derive(Eq)]", "#[derive(Foo(Bar))]", "no attrs here", "",
]


def test_attr_call_args_matches_regex():
    for t in _ATTR_CASES:
        for name in ("derive", "serde", "my_crate"):
            pat = re.compile(rf"#\[{re.escape(name)}\(([^)]+)\)")
            assert attr_call_args(t, name) == [m.group(1) for m in pat.finditer(t)], (t, name)


_DEFINES_CASES = [
    "struct Foo;", "enum Bar { A }", "trait Baz {}", "pub struct Foo;",
    "struct FooBar;", "mystruct Foo;", "struct  Foo;", "struct/*c*/Foo;",
    "type Foo = X;", "impl Foo {}", "let x: Foo;", "fn struct_foo(){}",
    "struct\nFoo;", "structFoo;", "trait Foo: Bar {}", "enum Foo<T> {}", "",
]


def test_defines_type_matches_regex():
    for t in _DEFINES_CASES:
        for name in ("Foo", "Bar", "Baz", "FooBar"):
            expected = bool(re.search(rf"\b(?:struct|enum|trait)\s+{re.escape(name)}\b", t))
            assert defines_type(t, name) == expected, (t, name)


_FN_SIG_LINES = [
    "fn foo() {", "pub fn foo() {", "async fn foo() {", "pub async fn foo() {",
    "    fn foo() {", "  pub  async   fn  foo() {", "fn  foo()", "fnfoo()",
    "public fn foo()", "pubfn foo()", "asyncfn foo()", "unsafe fn foo()",
    "pub unsafe fn foo()", "let f: fn() = g;", "fn", "fn ", "  fn", "async  fn x",
    "pub fn", "// fn foo", "struct Foo", "", "   ", "fnasync foo()",
]


def test_is_fn_signature_line_matches_regex():
    pat = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+")
    for line in _FN_SIG_LINES:
        assert is_fn_signature_line(line) == bool(pat.match(line)), line


def _old_impl_self_type(container: str) -> str | None:
    m = re.search(r"\bfor\s+([A-Za-z_]\w*)", container)
    if not m:
        m = re.search(r"\bimpl\s+(?:<[^>]*>\s+)?([A-Za-z_]\w*)", container)
    return m.group(1) if m else None


_IMPL_CASES = [
    "impl Foo", "impl Display for Foo", "impl Display for Foo<T>",
    "impl<T> Trait for Bar<T>", "impl <T> Foo", "impl <T: Clone> Foo",
    "impl Foo where T: Clone", "impl Trait for X where T: for<'a> Fn()",
    "impl 9bad", "for X", "impl", "", "impl _Priv for Q",
    "impl Tr<X> Foo", "impl for Foo",
]


def test_impl_self_type_no_regression_vs_regex():
    # Wherever the old regex matched, the structural parser returns the same type.
    for c in _IMPL_CASES:
        old = _old_impl_self_type(c)
        if old is not None:
            assert impl_self_type(c) == old, (c, old, impl_self_type(c))


def test_impl_self_type_fixes_nested_generics_and_spaceless():
    # Old single-`>` `<[^>]*>` returned "" (None) for nested own-generics; fixed.
    assert _old_impl_self_type("impl<T: Tr<X>> Foo") is None
    assert impl_self_type("impl<T: Tr<X>> Foo") == "Foo"
    # Old required `impl\s+` so a space-less `impl<T>` missed; fixed.
    assert _old_impl_self_type("impl<T> Foo") is None
    assert impl_self_type("impl<T> Foo") == "Foo"


_LINT_CASES = [
    "#![deny(warnings)]", "#![forbid(unsafe_code)]",
    "#![deny(missing_docs, unused)]", "#![deny()]", "#![deny(x]",
    "#![warn(unused)]", "#![deny(a)] #![forbid(b)]", "no attrs",
    "#![deny(a) ]", "#! [deny(a)]", "x #![forbid(c, d)] y", "",
    "#![deny(a)]#![forbid(b)]",
]


def test_crate_lint_attr_args_matches_regex():
    pat = re.compile(r"#!\[(?:deny|forbid)\(([^)]+)\)\]")
    for t in _LINT_CASES:
        assert crate_lint_attr_args(t) == [m.group(1) for m in pat.finditer(t)], t


_MIXED_CASES = [
    "Foo bar baz_qux", "fooBar AB_CD Foo_bar", "snake_case_99 X1 _priv",
    "camelCase HELLO a_b_c", "ab abc abcd", "a1b2 99 _under",
    "Result Option my_func TypeName", "x y z", "", "trailing_", "_leading",
    "Mixed123 under_score_end ALLCAPS",
]


def test_mixed_case_identifiers_matches_regex():
    pat = re.compile(r"\b([A-Z][a-zA-Z0-9]{1,}|[a-z][a-z0-9_]{2,}[a-z0-9])\b")
    for t in _MIXED_CASES:
        assert mixed_case_identifiers(t) == set(pat.findall(t)), t


_TOKEN_TEXTS = [
    "Foo::bar(x)", "9abc x9 _y", "Vec<Item>", "a_b_c 123 12d34",
    "Self::method()", "->i32", "", "fooBar baz9", "_::A", "x.y.z",
]


def test_identifier_tokens_matches_regex():
    pat = re.compile(r"[A-Za-z_]\w*")
    for t in _TOKEN_TEXTS:
        assert identifier_tokens(t) == pat.findall(t), t


def test_mask_identifiers_matches_regex():
    pat = re.compile(r"[A-Za-z_]\w*")
    for t in _TOKEN_TEXTS:
        assert mask_identifiers(t) == pat.sub("X", t), t
