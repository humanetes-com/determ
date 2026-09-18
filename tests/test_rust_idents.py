"""Unit tests for `pipeline_core.domain.rust_idents.fn_reference_counts`.

Pins byte-equivalence to the original regex pair it replaced
(`\\bfn\\s+([A-Za-z_]\\w*)` + per-name `\\b<name>\\b`) — the reference-frequency
map that drives leaf-first fill ordering must be unchanged.
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_idents import (
    attr_call_args,
    camelcase_type_names,
    canon_fn_name,
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
    impl_trait_name,
    is_fn_signature_line,
    is_trait_impl_container,
    is_unsafe_fn,
    mask_identifiers,
    mixed_case_identifiers,
    signature_fixed_by_trait,
    trait_def_name,
    use_path_requirement_gaps,
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


# --- source-regex→structured migrations (byte-equivalence pins) ---

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


_TRAIT_IMPLS = [
    "impl StdError for ContextError < C , E >",       # syn-spaced token stream
    "impl Display for X",                              # compact header
    "impl < 'a > Iterator for Chain < 'a >",           # spaced generics
    # trait impl whose WHERE clause carries a spaced HRTB — still a trait impl
    "impl < T > Trait for X where T : for < 'a > Fn ( & 'a str )",
]

_NOT_TRAIT_IMPLS = [
    "impl Foo",                                        # inherent impl
    "impl < T > Wrap < T >",                           # inherent, generic
    # inherent impl with a spaced-HRTB bound: `for` is followed by `<` → not flagged
    "impl < T > Wrap < T > where T : for < 'a > Fn ( & 'a str )",
    "impl Wrap where W : for<'a> Fn(&'a str)",         # compact HRTB: no bare `for`
    "trait TraitKind",                                 # trait DECLARATION
    "",                                                # free fn
    "for X",                                           # not an impl header
]


def test_is_trait_impl_container():
    for c in _TRAIT_IMPLS:
        assert is_trait_impl_container(c), c
    for c in _NOT_TRAIT_IMPLS:
        assert not is_trait_impl_container(c), c


# ---------------------------------------------------------------------------
# use_path_requirement_gaps — TASK 1.5 leaf-identifier matching
# ---------------------------------------------------------------------------

_FR_DOC = """# Functional Requirements: hardcrate

## Module: `cap`
- `wrapping_total(xs: &[u8]) -> u8` sums bytes with wrapping arithmetic.

## Module: `counter`
- `Counter::new(start: u32)` initializes the counter.

## Root Module
- `add(a: i32, b: i32) -> i32`
"""


def test_use_path_gaps_leaf_match_no_false_positives():
    # FR names symbols under module headings, never as `module::name` paths;
    # the leaf word must be enough (the run-421357 66/66-missing regression).
    tests = [("t.rs", "use hardcrate::cap::wrapping_total;\n"
                      "use hardcrate::{Counter, add};\n")]
    found, gaps = use_path_requirement_gaps(_FR_DOC, tests)
    assert found == 3
    assert gaps == []


def test_use_path_gaps_reports_genuinely_undocumented():
    tests = [("t.rs", "use hardcrate::framing::deframe;\n")]
    _found, gaps = use_path_requirement_gaps(_FR_DOC, tests)
    assert gaps == ["hardcrate::framing::deframe"]


def test_use_path_gaps_skips_std_glob_self_and_alias():
    tests = [("t.rs", "use std::fmt::Write;\n"
                      "use hardcrate::sprawl::*;\n"
                      "use hardcrate::counter::{self, Counter as C};\n")]
    _found, gaps = use_path_requirement_gaps(_FR_DOC, tests)
    # glob + self are never gaps; alias resolves to its leaf (`Counter`).
    assert gaps == []


def test_use_path_gaps_full_path_in_reqs_still_accepted():
    reqs = "The API exposes `hardcrate::framing::deframe` for decoding."
    tests = [("t.rs", "use hardcrate::framing::deframe;\n")]
    _found, gaps = use_path_requirement_gaps(reqs, tests)
    assert gaps == []


def test_canon_fn_name_strips_a_path_qualification():
    # A Rust ident cannot contain `:`, so a colon means the model answered with
    # a qualified name — the fn identity is the last segment.
    assert canon_fn_name("Error: root_cause") == "root_cause"
    assert canon_fn_name("Error::root_cause") == "root_cause"
    assert canon_fn_name("impl ErrorImpl: provide") == "provide"
    assert canon_fn_name("<Error as StdError>::source") == "source"
    assert canon_fn_name("  Error :: msg  ") == "msg"


def test_canon_fn_name_leaves_a_plain_name_and_an_unparsed_shape_alone():
    assert canon_fn_name("root_cause") == "root_cause"
    assert canon_fn_name("  from  ") == "from"
    assert canon_fn_name("_private") == "_private"
    # nothing identifier-shaped after the colon ⇒ return the input untouched
    # rather than inventing a name that would splice into the wrong place.
    assert canon_fn_name("Error: ") == "Error:"
    assert canon_fn_name("Error: 9lives") == "Error: 9lives"
    assert canon_fn_name("Error: root_cause()") == "Error: root_cause()"


def test_impl_self_type_embedded_for_is_not_the_keyword():
    # `for` as a SUBSTRING of an ident (Plat·for·m) must not cut — raw,
    # inherent, and canon store-key renderings alike.
    assert impl_self_type("impl Platform") == "Platform"
    assert impl_self_type("Platform<T>") == "Platform"
    assert impl_self_type("impl Iterator for PlatformIter") == "PlatformIter"
    assert impl_self_type("IteratorforPlatformIter") == "PlatformIter"
    assert impl_self_type("TransformforError") == "Error"
    # A trait name ending in `…for` glues a false candidate LEFT of the real
    # separator; the last valid `for` wins.
    assert impl_self_type("BufforforX") == "X"


def test_impl_self_type_canon_type_heads():
    # Self types a canon `for` may legitimately precede: primitives,
    # references, glued `dyn`/`mut` heads.
    assert impl_self_type("Displayforu8") == "u8"
    assert impl_self_type("impl Display for u8") == "u8"
    assert impl_self_type("Dereffor&Error") == "Error"
    assert impl_self_type("StdErrorfordynAny") == "Any"
    assert impl_self_type("impl StdError for dyn Any") == "Any"
    assert impl_self_type("impl Write for &mut W") == "W"


def test_impl_self_type_token_spaced_and_where():
    # Extractor token-spaced rendering and where-clause HRTBs.
    assert impl_self_type(
        "impl Context < T , E > for Result < T , E >") == "Result"
    assert impl_self_type(
        "impl<T> Trait for Foo<T> where T: for<'a> Fn(&'a str)") == "Foo"


# --- trait identity out of a container header ------------------------------

def test_impl_trait_name_reads_the_trait_a_header_implements() -> None:
    assert impl_trait_name(
        "impl<T> AdhocKind for &T\nwhere\n    T: ?Sized + Display,") == "AdhocKind"
    # generic trait: the args are not part of its identity
    assert impl_trait_name(
        "impl<T, E> Context<T, E> for Result<T, E>") == "Context"
    # path-qualified in the impl, bare in the declaration — same trait
    assert impl_trait_name("impl crate::kind::BoxedKind for Box<u8>") == "BoxedKind"
    # HRTB `for<'a>` is not the trait/type separator
    assert impl_trait_name("impl<F> Apply for F where F: for<'a> Fn(&'a u8)") == "Apply"


def test_impl_trait_name_none_for_non_trait_impls() -> None:
    assert impl_trait_name("impl Adhoc") is None
    assert impl_trait_name("trait AdhocKind") is None
    assert impl_trait_name("") is None


def test_trait_def_name_reads_a_declaration_header() -> None:
    assert trait_def_name("trait AdhocKind") == "AdhocKind"
    assert trait_def_name("pub trait AdhocKind: Sized") == "AdhocKind"
    assert trait_def_name("pub(crate) unsafe trait Raw<T>") == "Raw"
    assert trait_def_name("#[doc(hidden)]\npub trait TraitKind: Sized") == "TraitKind"


def test_trait_def_name_none_for_impls_and_other_items() -> None:
    # An `impl Trait for Ty` mentions a trait but declares none — routing a
    # default body there would write into an empty override.
    assert trait_def_name("impl<T> AdhocKind for &T") is None
    assert trait_def_name("impl Adhoc") is None
    assert trait_def_name("struct Adhoc") is None
    assert trait_def_name("") is None


def test_signature_fixed_by_trait_covers_declaration_and_impl() -> None:
    # A default method's bounds are the contract EVERY implementor is checked
    # against, so tightening them is E0276 the same as on an impl member.
    assert signature_fixed_by_trait("trait AdhocKind: Sized")
    assert signature_fixed_by_trait("impl Display for Error")
    assert not signature_fixed_by_trait("impl Error")
    assert not signature_fixed_by_trait("")


def test_repair_truncated_generics_rewrites_only_an_open_trailing_bracket() -> None:
    from pipeline_core.domain.rust_idents import repair_truncated_generics
    assert repair_truncated_generics("Context<T,E>forResult<T,E)") == \
        "Context<T,E>forResult<T,E>"
    assert repair_truncated_generics("StdErrorforContextError<C,Error]") == \
        "StdErrorforContextError<C,Error>"
    # Two lists left open, two wrong closers: both repaired.
    assert repair_truncated_generics("Afor<B<C))") == "Afor<B<C>>"
    # Balanced: untouched, `)` or not.
    assert repair_truncated_generics("Foofor(A,B)") == "Foofor(A,B)"
    assert repair_truncated_generics("DebugforError") == "DebugforError"
    # Open depth but no trailing closer to rewrite: returned as is.
    assert repair_truncated_generics("Foofor<T") == "Foofor<T"
