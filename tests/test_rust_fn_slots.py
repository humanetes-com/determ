"""Pins for `FnSlot` — a fn's identity is its owner chain plus its signature."""
from __future__ import annotations

from pipeline_core.domain.rust_fn_slots import fn_bodies

_SRC = """\
mod ext {
    pub trait Ctx {
        fn go(self) -> u32;
    }
    impl<E> Ctx for E
    where
        E: Copy,
    {
        fn go(self) -> u32 { 1 }
    }
    impl Ctx for Own {
        fn go(self) -> u32 { 2 }
    }
}
impl Debug for Thing {
    fn fmt(&self, f: &mut Formatter) -> Result { debug(f) }
}
impl Display for Thing {
    fn fmt(&self, f: &mut Formatter) -> Result { display(f) }
}
pub fn free() -> u32 { 3 }
"""


def test_same_signature_different_owners_are_distinct_slots() -> None:
    slots = [b.slot for b in fn_bodies(_SRC)]
    gos = [s for s in slots if s.signature.startswith("fn go")]
    assert len(gos) == 2
    assert gos[0] != gos[1]
    assert gos[0].signature == gos[1].signature   # the signature alone collides
    assert len({s.owner_path for s in gos}) == 2  # the owner chain separates them


def test_owner_path_is_the_enclosing_chain_outermost_first() -> None:
    by_owner = {b.slot.owner_path: b.slot.signature for b in fn_bodies(_SRC)}
    assert ("mod ext", "impl<E> Ctx for E where E: Copy,") in by_owner
    assert ("mod ext", "impl Ctx for Own") in by_owner
    assert ("impl Debug for Thing",) in by_owner
    assert ("impl Display for Thing",) in by_owner
    assert () in by_owner  # a free fn is owned by the file itself


def test_bodyless_trait_declaration_is_not_a_slot() -> None:
    """`fn go(self) -> u32;` has no body — taking the next `{` would swallow the
    following item and splicing into it would break the file."""
    sigs = [b.slot.signature for b in fn_bodies(_SRC)]
    assert sigs.count("fn go(self) -> u32") == 2  # the two impls, not the trait


def test_signature_is_whitespace_normalised_across_line_wrapping() -> None:
    wrapped = "impl A {\n    fn f(\n        x: u32,\n    ) -> u32 { x }\n}\n"
    inline = "impl A {\n    fn f( x: u32, ) -> u32 { x }\n}\n"
    assert (fn_bodies(wrapped)[0].slot.signature
            == fn_bodies(inline)[0].slot.signature)


def test_braces_in_strings_and_comments_do_not_desync_the_owner_stack() -> None:
    src = ('impl A {\n'
           '    fn f() -> &\'static str { "{{" }\n'
           '}\n'
           '// impl B { fn ghost() {} }\n'
           'impl C {\n'
           '    fn g() -> u32 { 1 }\n'
           '}\n')
    owners = {b.slot.owner_path for b in fn_bodies(src)}
    assert owners == {("impl A",), ("impl C",)}


def test_body_span_excludes_the_braces() -> None:
    src = "fn a() { 42 }\n"
    b = fn_bodies(src)[0]
    assert src[b.body_start:b.body_end] == " 42 "


def test_nested_fn_is_owned_by_its_enclosing_fn() -> None:
    src = "fn outer() {\n    fn inner() -> u32 { 1 }\n    inner();\n}\n"
    inner = [b for b in fn_bodies(src) if b.slot.signature.startswith("fn inner")]
    assert inner[0].slot.owner_path == ("fn outer()",)
