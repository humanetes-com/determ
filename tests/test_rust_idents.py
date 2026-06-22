"""Unit tests for `pipeline_core.domain.rust_idents.fn_reference_counts` (P7).

Pins byte-equivalence to the original regex pair it replaced
(`\\bfn\\s+([A-Za-z_]\\w*)` + per-name `\\b<name>\\b`) — the reference-frequency
map that drives leaf-first fill ordering must be unchanged.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_idents import (
    camelcase_type_names,
    declares_fn,
    defined_type_names,
    fn_reference_counts,
    is_unsafe_fn,
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
