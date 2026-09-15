"""Type SHAPE, not declaration text, decides which donor bodies the seed refuses.

Fixture: the declarations the two bakes of one crate carried when the second
cluster entered (2026-09-15, run `119837-09e3ac57`) — the first cluster's filled
tree against the second cluster's baked skeleton. The text rule refused every
body of the pointer file over spellings: an inline bound against a `where`
bound, a path spelled in full against the same path imported, a trailing comma,
and a `where T:` bound read as a field named `T`. The one type whose fields
really moved (`ErrorImpl`: a field's type changed and `#[repr(C)]` order
changed) must stay refused.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
from pipeline_core.domain.rust_item_shape import (
    WHOLE,
    item_shape,
    shape_divergence,
    use_bindings,
)
from pipeline_core.domain.rust_use_items import divergent_item_names

_DONOR_PTR = """\
#![allow(unused, dead_code, non_snake_case, clippy::all)]
use alloc::boxed::Box;
use core::marker::PhantomData;
use core::ptr::NonNull;
/// Owned non-null pointer used for Error's inner handle; Send+Sync via NonNull.
#[repr(transparent)]
pub struct Own<T: ?Sized> {
    pub ptr: core::ptr::NonNull<T>,
}
impl<T> Own<T>
where
    T: ?Sized,
{
    pub fn new(ptr: Box<T>) -> Self {
        Self { ptr: unsafe { NonNull::new_unchecked(Box::into_raw(ptr)) } }
    }
    pub unsafe fn boxed(self) -> Box<T> {
        Box::from_raw(self.ptr.as_ptr())
    }
}
/// Shared non-null reference used by ErrorImpl operations; size stays pointer-sized.
#[repr(transparent)]
pub struct Ref<'a, T: ?Sized> {
    pub ptr: core::ptr::NonNull<T>,
    lifetime: core::marker::PhantomData<&'a T>,
}
"""

_TARGET_PTR = """\
#![allow(unused, dead_code, non_snake_case, clippy::all)]
use alloc::boxed::Box;
use core::marker::PhantomData;
use core::ptr::NonNull;
#[repr(transparent)]
pub struct Own<T>
where
    T: ?Sized,
{
    pub ptr: NonNull<T>,
}
impl<T> Own<T>
where
    T: ?Sized,
{
    pub fn new(ptr: Box<T>) -> Self {
        ::core::unimplemented!()
    }
    pub unsafe fn boxed(self) -> Box<T> {
        ::core::unimplemented!()
    }
}
#[repr(transparent)]
pub struct Ref<'a, T>
where
    T: ?Sized,
{
    pub ptr: NonNull<T>,
    lifetime: PhantomData<&'a T>,
}
"""

_DONOR_ERROR = """\
use crate::ptr::{Own, Ref};
use crate::{Error, StdError};
use core::any::TypeId;
use core::mem::ManuallyDrop;
#[repr(C)]
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    _object: core::mem::ManuallyDrop<E>,
    backtrace: Option<Backtrace>,
}
struct ErrorVTable {
    object_ref: unsafe fn(Ref<ErrorImpl>) -> Ref<dyn crate::StdError + Send + Sync + 'static>,
    object_reallocate_boxed: unsafe fn(Own<ErrorImpl>) -> Box<dyn crate::StdError + Send + Sync + 'static>,
    object_downcast: unsafe fn(Ref<ErrorImpl>, core::any::TypeId) -> Option<Ref<()>>,
}
#[repr(transparent)]
pub struct Error {
    inner: Own<ErrorImpl>
}
"""

_TARGET_ERROR = """\
use crate::ptr::{Own, Ref};
use crate::{Error, StdError};
use core::any::TypeId;
use core::mem::ManuallyDrop;
#[repr(C)]
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Option<Backtrace>,
    _object: E,
}
struct ErrorVTable {
    object_ref: unsafe fn(Ref<ErrorImpl>) -> Ref<dyn StdError + Send + Sync + 'static>,
    object_reallocate_boxed: unsafe fn(
        Own<ErrorImpl>,
    ) -> Box<dyn StdError + Send + Sync + 'static>,
    object_downcast: unsafe fn(Ref<ErrorImpl>, TypeId) -> Option<Ref<()>>,
}
#[repr(transparent)]
pub struct Error {
    inner: Own<ErrorImpl>,
}
"""


def _text_rule(target: str, donor: str) -> frozenset[str]:
    """The pre-fix comparison, kept verbatim as the negative control: declared
    in both and not whitespace-identical -> the name plus every `ident:` token
    of either declaration."""
    from pipeline_core.domain.rust_scan import item_names, locate_item_declaration
    out: set[str] = set()
    for name in set(item_names(target)) & set(item_names(donor)):
        for kw in ("struct", "enum", "trait", "type", "union"):
            t = locate_item_declaration(target, kw, name)
            d = locate_item_declaration(donor, kw, name)
            if t is None or d is None:
                continue
            t_raw, d_raw = target[t[0]:t[1]], donor[d[0]:d[1]]
            if " ".join(t_raw.split()) != " ".join(d_raw.split()):
                out.add(name)
                for decl in (t_raw, d_raw):
                    for chunk in decl.replace("{", " ").replace("}", " ").replace(",", " ").split():
                        if chunk.endswith(":") and not chunk.endswith("::"):
                            out.add(chunk[:-1])
            break
    return frozenset(out)


def test_negative_control_the_text_rule_refuses_the_pointer_layer() -> None:
    div = _text_rule(_TARGET_PTR, _DONOR_PTR)
    assert {"Own", "Ref", "ptr", "T"} <= div
    assert {"Error", "ErrorVTable"} <= _text_rule(_TARGET_ERROR, _DONOR_ERROR)


def test_spelling_differences_are_not_a_shape() -> None:
    assert divergent_item_names(_TARGET_PTR, _DONOR_PTR) == frozenset()
    div = divergent_item_names(_TARGET_ERROR, _DONOR_ERROR)
    assert "Error" not in div and "inner" not in div          # trailing comma
    assert "ErrorVTable" not in div                            # imports + trailing comma
    assert not {"object_ref", "object_reallocate_boxed", "object_downcast"} & div


def test_a_where_bound_is_never_a_field() -> None:
    assert "T" not in divergent_item_names(_TARGET_PTR, _DONOR_PTR)
    assert "T" not in divergent_item_names(_TARGET_ERROR, _DONOR_ERROR)


def test_the_type_whose_fields_really_moved_stays_refused() -> None:
    div = divergent_item_names(_TARGET_ERROR, _DONOR_ERROR)
    assert {"ErrorImpl", "_object", "backtrace", "vtable"} <= div


def test_the_seed_adopts_the_pointer_layer_it_used_to_refuse() -> None:
    r = backfill_stub_bodies(_TARGET_PTR, [_DONOR_PTR])
    assert r.filled == 2
    assert r.layout_skipped == 0
    assert "Box::from_raw(self.ptr.as_ptr())" in r.text


def test_repr_c_order_is_a_layout_contract() -> None:
    a = "#[repr(C)]\nstruct S { a: u8, b: u16 }\n"
    b = "#[repr(C)]\nstruct S { b: u16, a: u8 }\n"
    sa, sb = item_shape(a, "struct", "S"), item_shape(b, "struct", "S")
    assert sa is not None and sb is not None
    assert shape_divergence(sa, sb) == WHOLE
    # Without repr(C) the compiler owns the order; nothing a body names moved.
    sa2 = item_shape("struct S { a: u8, b: u16 }\n", "struct", "S")
    sb2 = item_shape("struct S { b: u16, a: u8 }\n", "struct", "S")
    assert sa2 is not None and sb2 is not None
    assert shape_divergence(sa2, sb2) == frozenset()


def test_same_last_segment_through_different_imports_is_a_different_type() -> None:
    a = "use crate::Error;\nstruct W { e: Error }\n"
    b = "use core::error::Error;\nstruct W { e: Error }\n"
    assert divergent_item_names(a, b) == frozenset({"e"})


def test_a_one_tuple_is_not_its_parenthesised_element() -> None:
    a = "struct W { e: (u8,) }\n"
    b = "struct W { e: (u8) }\n"
    assert divergent_item_names(a, b) == frozenset({"e"})


def test_an_added_field_refuses_the_type_name_too() -> None:
    a = "struct W { a: u8, extra: u8 }\n"
    b = "struct W { a: u8 }\n"
    assert divergent_item_names(a, b) == frozenset({"W", "extra"})


def test_use_bindings_expand_groups_aliases_and_self() -> None:
    src = ("use crate::ptr::{Own, Ref};\nuse core::fmt::{self, Debug as D};\n"
           "use std::ptr::NonNull;\nuse crate::x::*;\n")
    b = use_bindings(src)
    assert b["Own"] == ("crate", "ptr", "Own")
    assert b["fmt"] == ("core", "fmt")
    assert b["D"] == ("core", "fmt", "Debug")
    assert b["NonNull"] == ("core", "ptr", "NonNull")


# --- the parser's own contract: what counts as the same declaration ---


def _shape(src: str, kind: str, name: str):  # type: ignore[no-untyped-def]
    s = item_shape(src, kind, name)
    assert s is not None
    return s


def test_comments_doc_attributes_and_literals_are_lexed_not_compared() -> None:
    a = ('/// doc\n#[doc = "x"]\n#[cfg(feature = "std")]\n'
         "struct S { /* nested /* block */ note */ a: u8, // tail\n b: [u8; 4] }\n")
    b = '#[cfg(feature = "std")]\nstruct S { a: u8, b: [u8; 4], }\n'
    assert shape_divergence(_shape(a, "struct", "S"), _shape(b, "struct", "S")) == frozenset()
    # A different string inside an attribute is a different attribute.
    c = '#[cfg(feature = "alloc")]\nstruct S { a: u8, b: [u8; 4] }\n'
    assert shape_divergence(_shape(a, "struct", "S"), _shape(c, "struct", "S")) == WHOLE


def test_char_literals_and_escaped_strings_stay_single_tokens() -> None:
    a = "enum E { A = b'x' as isize, B }\n"
    b = "enum E { A = b'y' as isize, B }\n"
    assert shape_divergence(_shape(a, "enum", "E"), _shape(b, "enum", "E")) == WHOLE
    s = '#[cfg(feature = "a\\"b")]\nstruct S { a: u8 }\n'
    assert _shape(s, "struct", "S").fields[0].name == "a"


def test_enum_variants_are_compared_as_a_body() -> None:
    a = "enum E<T> { A(T), B { x: u8 } }\n"
    b = "enum E<T> { A(T), B { x: u16 } }\n"
    assert divergent_item_names(a, b) == frozenset({"E", "x"})
    assert divergent_item_names(a, a) == frozenset()


def test_tuple_and_unit_structs() -> None:
    a = "pub struct W<T>(pub T, u8) where T: Clone;\n"
    b = "pub struct W<T: Clone>(pub T, u8,);\n"
    assert shape_divergence(_shape(a, "struct", "W"), _shape(b, "struct", "W")) == frozenset()
    c = "pub struct W<T: Clone>(T, u8);\n"
    assert shape_divergence(_shape(a, "struct", "W"), _shape(c, "struct", "W")) == WHOLE
    u = _shape("struct U;\n", "struct", "U")
    assert u.fields == () and u.tuple_fields == ()


def test_generic_params_lifetimes_consts_defaults_and_extra_predicates() -> None:
    a = ("struct G<'a, 'b: 'a, T: Copy + Clone = u8, const N: usize = 3>\n"
         "where Vec<T>: Send\n{ r: &'a [T; N], s: &'b T }\n")
    b = ("struct G<'a, 'b, T = u8, const N: usize = 3>\n"
         "where 'b: 'a, T: Clone + Copy, Vec<T>: Send,\n{ r: &'a [T; N], s: &'b T }\n")
    assert shape_divergence(_shape(a, "struct", "G"), _shape(b, "struct", "G")) == frozenset()
    c = b.replace("Vec<T>: Send", "Vec<T>: Sync")
    assert shape_divergence(_shape(a, "struct", "G"), _shape(c, "struct", "G")) == WHOLE
    d = b.replace("T: Clone + Copy", "T: Clone")
    assert shape_divergence(_shape(a, "struct", "G"), _shape(d, "struct", "G")) == WHOLE
    e = b.replace("T = u8", "T = u16")
    assert shape_divergence(_shape(a, "struct", "G"), _shape(e, "struct", "G")) == WHOLE


def test_a_where_predicate_without_a_colon_is_unparseable() -> None:
    assert item_shape("struct S<T> where T { a: T }\n", "struct", "S") is None
    assert item_shape("struct S { a: u8 }\n", "struct", "Missing") is None


def test_unparseable_declarations_fall_back_to_a_whole_type_text_rule() -> None:
    a = "pub trait K: Sized { fn k(&self) -> u8 { 1 } }\n"
    b = "pub trait K: Sized { fn k(&self) -> u8 { 2 } }\n"
    assert item_shape(a, "trait", "K") is None
    assert divergent_item_names(a, b) == frozenset({"K"})
    assert divergent_item_names(a, a) == frozenset()


def test_field_visibility_and_attributes_are_part_of_a_field() -> None:
    a = "use crate::x::Y;\nstruct S { pub(crate) a: Y, #[cfg(feature = \"std\")] b: u8 }\n"
    b = "struct S { a: crate::x::Y, #[cfg(feature = \"std\")] b: u8 }\n"
    assert divergent_item_names(a, b) == frozenset({"a"})
    c = "use crate::x::Y;\nstruct S { pub(crate) a: Y, b: u8 }\n"
    assert divergent_item_names(a, c) == frozenset({"b"})


def test_paths_resolve_through_roots_aliases_and_local_items() -> None:
    a = ("use std::fmt::Debug as D;\nstruct L;\n"
         "struct S { a: ::core::fmt::Debug, b: L, c: self::L, d: Option<u8>, e: other::T }\n")
    b = ("struct L;\n"
         "struct S { a: alloc::fmt::Debug, b: self::L, c: L, d: core::option::Option<u8>, e: other::T }\n")
    assert divergent_item_names(a, b) == frozenset()
    c = b.replace("e: other::T", "e: another::T")
    assert divergent_item_names(a, c) == frozenset({"e"})
    # A path type is never equal to a bare token in the same position.
    d = b.replace("b: self::L", "b: u8")
    assert "b" in divergent_item_names(a, d)


def test_use_bindings_drop_clashes_underscores_globs_and_absolute_prefixes() -> None:
    src = ("#[cfg(a)]\nuse crate::one::Thing;\n#[cfg(not(a))]\nuse crate::two::Thing;\n"
           "use crate::t::Trait as _;\nuse ::core::cell::Cell;\nuse crate::g::*;\n"
           "use crate::p::{self as q};\n")
    b = use_bindings(src)
    assert "Thing" not in b
    assert "_" not in b
    assert b["Cell"] == ("core", "cell", "Cell")
    assert b["q"] == ("crate", "p")


def test_bag_equality_needs_every_bound_matched() -> None:
    a = "struct S<T: Clone + Send> { a: T }\n"
    b = "struct S<T: Clone + Sync> { a: T }\n"
    assert shape_divergence(_shape(a, "struct", "S"), _shape(b, "struct", "S")) == WHOLE


def test_declaration_field_names_read_the_body_only() -> None:
    from pipeline_core.domain.rust_item_shape import declaration_field_names
    assert declaration_field_names(None, "enum E<T> where T: Copy { A { x: T }, B(u8) }") == {"x"}
    assert declaration_field_names(None, "type A = B;") == set()
