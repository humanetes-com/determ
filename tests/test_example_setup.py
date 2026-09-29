"""Pins for `domain/example_setup.py` on invented shapes: statement splitting,
what a statement binds, the def-use slice, and the file-level items a test
example can call. The recorded-file claims live in tests/test_behaviour_book.py
and tests/test_test_assertions.py; these cover the shapes those files do not."""
from __future__ import annotations

from pipeline_core.domain.example_setup import (
    binds,
    fn_body_span,
    helper_closure,
    is_observation,
    item_index,
    mod_declarations,
    mut_binds,
    setup_slice,
    statements,
    top_level_items,
    use_block,
)


def test_a_fn_without_a_body_has_no_statements() -> None:
    assert fn_body_span("struct S;") is None
    assert fn_body_span("fn f(;") is None
    assert fn_body_span("fn f(a: u8);") is None
    assert fn_body_span("fn f(a: u8) -> u8") is None
    assert statements("trait T { fn f(); }") == []
    assert setup_slice("fn t() { assert!(x); }", 1000) == ""


def test_block_statements_end_at_their_brace_unless_else_follows() -> None:
    item = """fn t() {
    // a comment line
    #[allow(unused)]
    let a = 1;
    if a > 0 { go(); } else { stop(); }
    { let inner = 2; }
    for i in 0..a { use_it(i); }
    a
}"""
    texts = [s.text.strip() for s in statements(item)]
    assert texts == ["#[allow(unused)]\n    let a = 1;",
                     "if a > 0 { go(); } else { stop(); }",
                     "{ let inner = 2; }",
                     "for i in 0..a { use_it(i); }",
                     "a"]


def test_what_a_statement_binds() -> None:
    assert binds("let (a, mut b): (A, B) = make();") == {"a", "b"}
    assert binds("let Some(x) = opt else { return };") == {"x"}
    assert binds("let v: Vec<u8> = vec![];") == {"v"}
    assert binds("unsafe fn danger() {}") == {"danger"}
    assert binds("unsafe { poke(p) };") == frozenset()
    assert binds("impl Display for Wrapper { }") == {"Wrapper"}
    assert binds("impl Local { }") == {"Local"}
    assert binds("use std::fmt::{self, Display};") >= {"Display", "fmt"}
    assert binds("#[derive(Debug)] struct Local;") == {"Local"}
    assert binds("#[unclosed") == frozenset()
    assert binds("x.push(1);") == frozenset()
    assert mut_binds("let (mut a, b) = pair();") == {"a"}
    assert mut_binds("x = 1;") == frozenset()


def test_observations_are_not_setup() -> None:
    assert is_observation("assert!(x);", frozenset())
    assert is_observation("check(a, \"b\");", frozenset({"check"}))
    assert not is_observation("check_not(a);", frozenset({"check"}))


def test_the_slice_keeps_local_items_mutations_and_the_initializer_reads() -> None:
    item = """fn t() {
    fn local() -> u8 { 1 }
    let n: u8 = local();
    let mut v = Vec::new();
    v.push(n);
    let unrelated = 5;
    let x;
    assert_eq!("[1]", format!("{:?}", v));
}"""
    got = setup_slice(item, item.index("assert_eq!"))
    assert got == ("fn local() -> u8 { 1 }\nlet n: u8 = local();\nlet mut v = Vec::new();\n"
                   "v.push(n);")


def test_top_level_items_skip_uses_and_declarations_and_bind_declared_types() -> None:
    src = """#![allow(dead_code)]
use std::fmt;
pub use crate::x;
mod common;
pub(crate) fn helper() {}
extern "C" fn ffi() {}
pub unsafe fn raw() {}
const LIMIT: &str = "x";
declare!(Alpha, Beta);
something_else;
impl<T> Trait for Holder<T> {}
#[test]
fn t() {}
"""
    items = [(i.name, i.is_test, i.is_impl) for i in top_level_items(src)]
    assert items == [("helper", False, False), ("ffi", False, False), ("raw", False, False),
                     ("LIMIT", False, False), ("Alpha", False, False), ("Beta", False, False),
                     ("Holder", False, True), ("t", True, False)]
    assert mod_declarations(src) == ["common"]
    assert use_block(src) == "use std::fmt;\npub use crate::x;"


def test_helper_closure_follows_calls_and_skips_tests() -> None:
    a = "fn a() { b() }\nfn b() { 1 }\n#[test]\nfn c() {}\nstruct S;\nimpl S { fn m(&self) {} }\n"
    idx = item_index([("tests/a.rs", a)])
    got = helper_closure("let x = a(); let s = S; c();", idx)
    assert [k for k, _ in got] == ["tests/a.rs::S", "tests/a.rs::S::impl1", "tests/a.rs::a", "tests/a.rs::b"]
    assert helper_closure("nothing()", idx, extra=("a",))[0][0] == "tests/a.rs::a"
