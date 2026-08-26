"""What a generated test block reads that is not `pub` — pure domain.

The instance: `src/error.gen_tests_3.rs:29` reads `err.inner.ptr.as_ptr()` on
`Error`, whose `inner` is private. The block was admitted (it compiled and
passed), became write-protected oracle content, and the first authored layout
without an `inner` field failed all 56 unit tests of both clusters. The
contract is PUBLIC API ONLY; this is the check that names the violation.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.oracle_visibility import (
    declared_items,
    fn_line_spans,
    names_private_items,
    public_item_names,
)

_MICROCRATE = Path(__file__).parent / "fixtures" / "microcrate" / "src"

_FIXTURE = """\
use std::fmt;

/// Public struct with a private field and a public one.
pub struct Error {
    inner: Own<ErrorImpl>,
    pub code: u32,
}

struct Own<T> {
    ptr: NonNull<T>,
}

pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    _object: E,
}

enum Kind { Adhoc, Boxed }

pub struct Pair(pub u8, u8);

impl Error {
    pub fn msg<M>(message: M) -> Self { unimplemented!() }
    fn vtable_ptr(&self) -> usize { 0 }
    pub(crate) fn kind(&self) -> Kind { Kind::Adhoc }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { Ok(()) }
}

fn helper() -> u32 { 1 }
pub fn exported() -> u32 { helper() }

#[cfg(test)]
mod tests {
    use super::*;
    fn only_in_tests() {}
    #[test]
    fn t() { assert_eq!(helper(), 1); }
}
"""


def test_declared_items_carry_visibility_owner_and_fields() -> None:
    items = {(it.kind, it.name): it for it in declared_items(_FIXTURE)}
    assert items[("struct", "Error")].is_pub
    assert items[("struct", "Error")].fields == (("inner", False), ("code", True))
    assert not items[("struct", "Own")].is_pub
    assert items[("struct", "ErrorImpl")].is_pub          # pub(crate) counts as pub
    assert not items[("enum", "Kind")].is_pub
    assert items[("struct", "Pair")].tuple_fields == (True, False)
    assert items[("fn", "msg")].is_pub and items[("fn", "msg")].owner == "Error"
    assert not items[("fn", "vtable_ptr")].is_pub
    assert items[("fn", "kind")].is_pub
    assert not items[("fn", "helper")].is_pub
    assert ("fn", "fmt") not in items                     # trait impl method
    assert ("fn", "only_in_tests") not in items           # cfg(test) module
    assert ("fn", "t") not in items
    assert public_item_names(declared_items(_FIXTURE)) >= {"Error", "ErrorImpl", "msg", "exported"}


def test_fn_line_spans_start_at_the_signature_line() -> None:
    spans = fn_line_spans(_FIXTURE)
    lines = _FIXTURE.splitlines()
    assert lines[spans["msg"][0] - 1].strip().startswith("pub fn msg")
    assert lines[spans["helper"][0] - 1].startswith("fn helper")
    assert spans["helper"] == (spans["helper"][0], spans["helper"][0])   # one-line fn


def test_the_live_private_field_read_is_named() -> None:
    block = '''#[test]
fn test_errorimpl_debug_fmt() {
    let err = Error::msg("impl debug test");
    // SAFETY: Error's inner pointer points to a valid heap allocation.
    let heap_ptr: *const ErrorImpl = err.inner.ptr.as_ptr();
    assert!(format!("{:?}", heap_ptr).contains("impl"));
}
'''
    assert names_private_items(block, _FIXTURE) == ("Error.inner.ptr",)


def test_a_block_on_the_public_api_names_nothing() -> None:
    block = '#[test]\nfn t() { assert!(Error::msg("x").is::<&str>()); assert_eq!(exported(), 1); }'
    assert names_private_items(block, _FIXTURE) == ()
    # a public field read, a pub(crate) method call, a std type sharing a
    # private name's spelling behind a path: none of them is private.
    block = '#[test]\nfn t() { let e = Error::msg("x"); let _ = e.code; let _ = e.kind(); let _: fmt::Formatter; }'
    assert names_private_items(block, _FIXTURE) == ()


def test_private_fn_type_and_tuple_construction_are_named() -> None:
    block = ('#[test]\nfn t() {\n    let e = Error::msg("x");\n    let _ = e.vtable_ptr();\n'
             '    let _ = helper();\n    let k = Kind::Adhoc;\n    let p = Pair(1, 2);\n}')
    assert names_private_items(block, _FIXTURE) == (
        "Pair(..)", "enum Kind", "fn helper", "fn vtable_ptr")


def test_private_field_in_a_struct_literal_and_untyped_read_are_named() -> None:
    counter = (_MICROCRATE / "counter.rs").read_text(encoding="utf-8")
    literal = '#[test]\nfn t() { let c = Counter { value: 3 }; assert_eq!(c.get(), 3); }'
    assert names_private_items(literal, counter) == ("Counter.value",)
    untyped = '#[test]\nfn t() { let c = make(); assert_eq!(c.value, 3); }'
    assert names_private_items(untyped, counter) == ("Counter.value",)
    public = '#[test]\nfn t() { let mut c = Counter::new(1); assert_eq!(c.bump(), 2); }'
    assert names_private_items(public, counter) == ()


def test_prose_and_strings_never_name_a_private_item() -> None:
    block = ('#[test]\nfn t() {\n    // helper() is what exported() calls; Kind is private\n'
             '    let s = "helper() and Kind";\n    assert_eq!(exported(), 1);\n}')
    assert names_private_items(block, _FIXTURE) == ()
