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
    declares_fn,
    defined_type_names,
    defines_type,
    first_fn_decl_name,
    fn_decl_names,
    fn_reference_counts,
    is_fn_signature_line,
    is_unsafe_fn,
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
