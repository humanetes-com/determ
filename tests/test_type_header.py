"""Pins for the frozen-head / authorable-fields split of a type declaration.

The head is the public contract; the fields are not. Every assertion here is
about keeping that line in the right place — a head that compares equal when a
bound moved would let an authored layout silently change the crate's API.
"""
from __future__ import annotations

from pipeline_core.domain.rust_elide import elide_private_struct_fields
from pipeline_core.domain.type_header import (
    declares_pub_field,
    field_block_span,
    headers_match,
    parse_type_header,
)


def test_head_parses_past_attributes_and_keeps_bounds():
    h = parse_type_header(
        "#[repr(transparent)]\n#[derive(Debug)]\npub struct Handle<T: ?Sized> {\n"
        "    inner: Box<T>,\n}")
    assert h is not None
    assert (h.vis, h.kind, h.name, h.generics) == (
        "pub", "struct", "Handle", "<T:?Sized>")


def test_heads_match_modulo_whitespace_only():
    frozen = parse_type_header("pub struct Pair<A, B> { a: A, b: B }")
    assert headers_match(frozen, parse_type_header("pub struct Pair<A,B> { x: (A, B) }"))
    # a dropped bound, a renamed lifetime and a changed visibility are all
    # different public types, however similar they read.
    assert not headers_match(
        parse_type_header("pub struct H<T: ?Sized> { i: Box<T> }"),
        parse_type_header("pub struct H<T> { i: Box<T> }"))
    assert not headers_match(
        parse_type_header("pub struct C<'a> { s: &'a [u8] }"),
        parse_type_header("pub struct C<'s> { s: &'s [u8] }"))
    assert not headers_match(
        parse_type_header("pub struct X { a: u8 }"),
        parse_type_header("pub(crate) struct X { a: u8 }"))


def test_fn_bound_arrow_does_not_close_the_generics():
    h = parse_type_header("pub struct Lazy<F: Fn(u8) -> u8> { f: F }")
    assert h is not None and h.generics == "<F:Fn(u8)->u8>"


def test_where_clause_parens_are_not_a_tuple_field_list():
    text = "pub struct S<T> where T: Fn(u8) { inner: T }"
    span = field_block_span(text)
    assert span is not None
    assert text[span[0]] == "{"
    assert text[span[0] + 1:span[1]].strip() == "inner: T"


def test_tuple_and_unit_struct_field_blocks():
    assert field_block_span("struct Own<T>(NonNull<T>);") is not None
    assert field_block_span("pub struct Marker;") is None


def test_pub_field_detection_ignores_comments_and_the_head():
    assert not declares_pub_field("pub struct E { inner: u8 } // pub")
    assert not declares_pub_field("pub struct E { /* pub */ inner: u8 }")
    assert declares_pub_field("pub struct E { pub inner: u8 }")
    assert declares_pub_field("pub struct Own<T>(pub NonNull<T>);")


def test_elide_keeps_the_head_and_only_touches_named_structs():
    src = ("fn f() { struct Local { a: u8 } }\n"
           "pub struct Chain<'a> { state: ChainState<'a> }\n"
           "struct Other { b: u8 }\n")
    out = elide_private_struct_fields(src, {"Chain", "Local"})
    assert "pub struct Chain<'a> { /* private fields */ }" in out
    assert "state: ChainState" not in out
    # fn-body blind: a fn-local struct is not the file's layout, and an
    # unnamed struct is nobody's business here.
    assert "struct Local { a: u8 }" in out
    assert "struct Other { b: u8 }" in out


def test_elide_leaves_a_parseable_tuple_struct():
    out = elide_private_struct_fields("pub struct Own<T>(NonNull<T>);", {"Own"})
    assert out.startswith("pub struct Own<T>(")
    assert out.endswith(");")
    assert "NonNull" not in out


def test_head_parse_survives_annotated_and_truncated_text():
    """These parsers also read MODEL output and annotated stubs (failed attempts
    are kept as comments, cargo error text included), so malformed input must
    degrade to a verdict, never to a wrong head."""
    assert parse_type_header("// pub struct Ghost { a: u8 }\nstruct Real { b: u8 }"
                             ).name == "Real"
    assert parse_type_header("#![allow(dead_code)]\n#[derive(Debug)]\n"
                             "pub struct S { a: u8 }").name == "S"
    assert parse_type_header("fn f() -> u8 { 1 }") is None
    assert parse_type_header("pub struct") is None
    # truncated generics/body: parse what is there, do not raise or hang
    assert parse_type_header("pub struct S<T").generics == "<T"
    assert field_block_span("pub struct S") is None
    assert field_block_span("pub struct S {") == (13, 13)


def test_field_block_span_skips_leading_comments_and_attributes():
    text = "/* c */ #[repr(C)] struct S { a: u8 }"
    span = field_block_span(text)
    assert span is not None and text[span[0]] == "{"


def test_unit_struct_has_no_pub_field_and_survives_elision():
    assert not declares_pub_field("pub struct Marker;")
    assert elide_private_struct_fields("pub struct Marker;", {"Marker"}) == (
        "pub struct Marker;")
