"""Unit tests for the pure prompt formatters in pipeline_core.prompts.sections:
format_type_for_prompt (TypeRecord → Rust-like def) and
format_impl_block_for_prompt (ImplBlock → target-form member listing).
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.models import (
    FieldRecord,
    FnRecord,
    ImplBlock,
    MethodRecord,
    TypeRecord,
)
from pipeline_core.prompts.sections import (
    format_impl_block_for_prompt,
    format_type_for_prompt,
)


def test_format_type_struct_with_fields():
    rec = TypeRecord(kind="struct", name="Cfg", generics="<T>",
                     fields=(FieldRecord(name="n", ty="usize", is_pub=True),
                             FieldRecord(name="hidden", ty="T")))
    out = format_type_for_prompt(rec)
    assert "struct Cfg<T>" in out
    assert "pub n: usize" in out
    assert "hidden: T" in out
    assert "fields (access with .name)" in out


def test_construct_hint_tuple_struct_shows_positional_not_new():
    rec = TypeRecord(kind="struct", name="ContextError", generics="<C, E>",
                     definition="struct ContextError<C, E>(C, E)",
                     fields=(FieldRecord(name="0", ty="C"),
                             FieldRecord(name="1", ty="E")))
    out = format_type_for_prompt(rec)
    assert "construct: ContextError(<C>, <E>)" in out
    assert "NOT ContextError::new" in out


def test_construct_hint_named_struct_shows_literal():
    rec = TypeRecord(kind="struct", name="Error",
                     definition="struct Error { inner: Box<ErrorImpl> }",
                     fields=(FieldRecord(name="inner", ty="Box<ErrorImpl>"),))
    out = format_type_for_prompt(rec)
    assert "construct: Error { inner: <Box<ErrorImpl>> }" in out
    assert "NOT Error::new" in out


def test_construct_hint_tuple_struct_without_field_records():
    rec = TypeRecord(kind="struct", name="Wrapper",
                     definition="struct Wrapper(Box<dyn Error>)")
    out = format_type_for_prompt(rec)
    assert "construct: Wrapper(<fields...>)" in out


def test_construct_hint_absent_for_enum():
    rec = TypeRecord(kind="enum", name="Kind", variants=("A", "B"))
    assert "construct:" not in format_type_for_prompt(rec)


def test_format_type_enum_truncates_variants():
    rec = TypeRecord(kind="enum", name="E",
                     variants=tuple(f"V{i}" for i in range(12)))
    out = format_type_for_prompt(rec)
    assert "variants:" in out
    assert "V0" in out and "V7" in out
    assert "... (4 more)" in out  # only first 8 shown


def test_format_type_methods_dedup_truncate_and_overflow():
    methods = [MethodRecord(name="new", sig="fn new() -> Self", is_pub=True),
               MethodRecord(name="new", sig="fn new(dup)")]  # dup name dropped
    methods += [MethodRecord(name=f"m{i}", sig=f"fn m{i}()") for i in range(12)]
    methods.append(MethodRecord(name="big", sig="fn big(" + "x" * 200 + ")"))
    rec = TypeRecord(kind="struct", name="S", methods=tuple(methods))
    out = format_type_for_prompt(rec)
    assert "methods (sig shown" in out
    assert "pub fn new() -> Self" in out
    assert out.count("fn new(") == 1  # dedup kept first only
    assert "// also:" in out  # >10 methods → overflow line


def test_format_type_method_without_sig():
    rec = TypeRecord(kind="struct", name="S",
                     methods=(MethodRecord(name="go", sig="", is_exactly_pub=False),))
    out = format_type_for_prompt(rec)
    assert "fn go(...)  // sig unknown" in out


def test_format_type_empty_record():
    out = format_type_for_prompt(TypeRecord())
    assert out == "? ?"  # kind/name default to "?"


def test_format_type_pub_crate_method_not_rendered_as_pub():
    # `pub(crate) fn helper(...)` is NOT part of the crate's
    # external API — rendering it as "pub fn ..." is a factually wrong claim
    # the model would act on. is_pub folds pub(crate) in; is_exactly_pub must
    # be what gates the literal "pub " text.
    rec = TypeRecord(kind="struct", name="S", methods=(
        MethodRecord(name="helper", sig="fn helper(&self)",
                     is_pub=True, is_exactly_pub=False),))
    out = format_type_for_prompt(rec)
    assert "fn helper(&self)" in out
    assert "pub fn helper" not in out


def _fn(name, body="", stub=False):
    return FnRecord(fn_name=name, sig_text=f"fn {name}(&self)", body_text=body,
                    has_unimplemented_body=stub)


def test_format_impl_block_stub_and_filled():
    block = ImplBlock(container="impl Foo {",
                      members=(_fn("a", stub=True), _fn("b", body="return 1;")))
    out = format_impl_block_for_prompt(block, mark_target=True)
    assert "impl Foo {" in out
    assert "[STUB] — fill in this response" in out
    assert "/* FILLED */" in out and "return 1;" in out
    assert out.rstrip().endswith("}")


def test_format_impl_block_stub_unmarked():
    block = ImplBlock(container="impl Foo", members=(_fn("a", stub=True),))
    out = format_impl_block_for_prompt(block, mark_target=False)
    assert "// [STUB]" in out
    assert "fill in this response" not in out


def test_format_impl_block_long_body_preview_truncated():
    long_body = "\n".join(f"line{i};" for i in range(60))
    block = ImplBlock(container="impl Foo {", members=(_fn("a", body=long_body),))
    out = format_impl_block_for_prompt(block)
    assert "/* ... */" in out  # body preview capped at 200 chars


def test_format_impl_block_free_fns_no_container():
    block = ImplBlock(container="", members=(_fn("free", stub=True),))
    out = format_impl_block_for_prompt(block)
    assert "// free fns at file level" in out
    assert "[STUB]" in out


# --- what the TARGET render may claim about a member ----------------------- #

def test_format_impl_block_cfg_dead_stub_is_not_asked_for():
    """`provide` under the stable cluster has no body and is never compiled
    there; rendering it "[STUB] — fill in this response" while the header
    listed only `source` had the model answer it and the splice refuse the
    body (`not_in_cluster_cfg`, four blocks on 2026-08-20)."""
    block = ImplBlock(container="impl<C, E> StdError for ContextError<C, E> {",
                      members=(_fn("source", stub=True), _fn("provide", stub=True)))
    out = format_impl_block_for_prompt(
        block, mark_target=True, fill_fn_names=frozenset({"source"}))
    src = next(ln for ln in out.splitlines() if "fn source" in ln)
    prov = next(ln for ln in out.splitlines() if "fn provide" in ln)
    assert "fill in this response" in src
    assert "NOT in this response" in prov and "do NOT emit an <FN>" in prov
    assert "fill in this response" not in prov
    # Legacy reading (no fill_fn_names): every stub is a target.
    legacy = format_impl_block_for_prompt(block, mark_target=True)
    assert legacy.count("fill in this response") == 2
    # A related render never asks for anything.
    related = format_impl_block_for_prompt(block, mark_target=False,
                                           fill_fn_names=frozenset({"source"}))
    assert "this response" not in related


def test_format_impl_block_inferred_refill_is_hedged_not_convicted():
    """A re-fill stamped by inference only (escalation / traversal step) is
    `[REFILL?]` with an instruction to return a right body unchanged — the
    certainty tag had the correct `ContextError<C, E>::source` rewritten."""
    block = ImplBlock(container="impl<C, E> StdError for ContextError<C, E> {",
                      members=(_fn("source", body="Some(&self.error)"),))
    out = format_impl_block_for_prompt(
        block, refill_fn_names=frozenset({"source"}),
        inferred_fn_names=frozenset({"source"}))
    assert "[REFILL?]" in out and "return it unchanged" in out
    assert "Some(&self.error)" in out
    assert "its test below FAILS" not in out
    direct = format_impl_block_for_prompt(block, refill_fn_names=frozenset({"source"}))
    assert "[REFILL] compiles but its test below FAILS" in direct
    assert "[REFILL?]" not in direct


def test_format_impl_block_aimed_callee_is_not_hedged():
    """An aimed producer is a callee of the faulting frame, not a name
    match. `[REFILL?]` is the instruction that kept `object_downcast`
    byte-identical across three 122 sweeps of run `1911637-d0fab212`."""
    block = ImplBlock(container="",
                      members=(_fn("object_downcast", body="addr_of!(_object)"),))
    named = format_impl_block_for_prompt(
        block, refill_fn_names=frozenset({"object_downcast"}),
        inferred_fn_names=frozenset({"object_downcast"}))
    assert "[REFILL?]" in named and "return it unchanged" in named
    aimed = format_impl_block_for_prompt(
        block, refill_fn_names=frozenset({"object_downcast"}),
        inferred_fn_names=frozenset({"object_downcast"}),
        aimed_fn_names=frozenset({"object_downcast"}))
    assert "[REFILL?]" not in aimed
    assert "callee of the faulting frame" in aimed
    assert "do NOT return it unchanged" in aimed


def test_format_impl_block_related_candidate_is_shown_in_full():
    """In a RELATED block, the same-named re-fill candidate is the competing
    hypothesis — full body, named as a candidate, not a truncated
    `/* FILLED */` preview that reads as settled."""
    long_body = "\n".join(f"line{i};" for i in range(60))
    block = ImplBlock(container="impl<C> StdError for ContextError<C, Error> {",
                      members=(_fn("source", body=long_body),))
    out = format_impl_block_for_prompt(
        block, mark_target=False, candidate_fn_names=frozenset({"source"}))
    assert "same-named re-fill candidate" in out
    assert "line59;" in out and "/* ... */" not in out
    plain = format_impl_block_for_prompt(block, mark_target=False)
    assert "/* FILLED */" in plain and "/* ... */" in plain


def test_format_impl_block_companion_is_quoted_not_refill():
    """A crash-recovered table slot whose tests already pass is shown in
    full with an instruction not to emit an FN. Marking it [REFILL]
    rewrite is how a pair fill reddened tests that slot already paid.
    Control: the same member in refill_fn_names is still a rewrite."""
    body = "let ptr = e.as_ptr();\nunsafe { (*ptr).as_ref() }"
    block = ImplBlock(container="",
                      members=(_fn("slot", body=body),))
    quoted = format_impl_block_for_prompt(
        block, companion_fn_names=frozenset({"slot"}))
    assert "this pair will call this body" in quoted
    assert "do NOT emit an <FN> for it" in quoted
    assert "unsafe { (*ptr).as_ref() }" in quoted
    assert "its test below FAILS" not in quoted
    refill = format_impl_block_for_prompt(
        block, refill_fn_names=frozenset({"slot"}))
    assert "[REFILL] compiles but its test below FAILS" in refill
    assert "do NOT emit an <FN> for it" not in refill


def test_repr_transparent_rendered_above_the_type_line():
    # A producer rewrite reasoning about a transparent wrapper needs the repr
    # to know the cast (object_downcast::<M>) is sound. The compact type line
    # dropped it; now it is rendered from `definition`.
    rec = TypeRecord(
        kind="struct", name="MessageError", generics="<M>",
        definition="#[repr(transparent)]\npub struct MessageError<M>(pub M);",
        fields=(FieldRecord(name="0", ty="M", is_pub=True),))
    out = format_type_for_prompt(rec)
    assert "#[repr(transparent)]" in out
    # It precedes the struct line.
    assert out.index("#[repr(transparent)]") < out.index("struct MessageError<M>")


def test_derive_kept_repr_and_derive_only_cfg_dropped():
    rec = TypeRecord(
        kind="struct", name="Foo", generics="",
        definition=('#[cfg(feature = "std")]\n#[derive(Clone, Debug)]\n'
                    "#[repr(C)]\npub struct Foo { a: u8 }"),
        fields=(FieldRecord(name="a", ty="u8"),))
    out = format_type_for_prompt(rec)
    assert "#[derive(Clone, Debug)]" in out
    assert "#[repr(C)]" in out
    assert "#[cfg" not in out          # cfg gates presence, not the cast/trait contract


def test_no_attrs_when_definition_bare():
    rec = TypeRecord(kind="struct", name="Bare",
                     definition="pub struct Bare { a: u8 }",
                     fields=(FieldRecord(name="a", ty="u8"),))
    out = format_type_for_prompt(rec)
    assert out.startswith("struct Bare")
    assert "#[" not in out


def test_repr_behind_doc_comments_is_still_found():
    # The extractor renders `definition` with prettyplease, which prints the
    # doc comments FIRST and the attributes after them — so a documented type
    # reads `/// …` then `#[repr(…)]`. A scan that stops at the first
    # non-`#[` line would hide every documented type's repr behind its docs.
    rec = TypeRecord(
        kind="struct", name="Chain", generics="<'a>",
        definition=("/// Iterator of a chain of source errors.\n"
                    "///\n"
                    "/// This type is the iterator returned by [`Error::chain`].\n"
                    "#[derive(Clone)]\n"
                    "#[repr(transparent)]\n"
                    "pub struct Chain<'a> { state: ChainState<'a> }"),
        fields=(FieldRecord(name="state", ty="ChainState<'a>"),))
    out = format_type_for_prompt(rec)
    assert "#[derive(Clone)]" in out and "#[repr(transparent)]" in out
    assert out.index("#[repr(transparent)]") < out.index("struct Chain")
    # The docs used to be dropped here (this line asserted `"///" not in
    # out`); they are the contract channel now and print ABOVE the attrs,
    # with the blank `///` separator skipped.
    assert out.splitlines()[:2] == [
        "/// Iterator of a chain of source errors.",
        "/// This type is the iterator returned by [`Error::chain`].",
    ]
    assert out.index("///") < out.index("#[derive(Clone)]")


def test_multi_line_attribute_is_joined_before_the_head_is_read():
    rec = TypeRecord(
        kind="struct", name="Wide", generics="",
        definition=("#[derive(\n    Clone,\n    Debug,\n)]\n"
                    "#[repr(C)]\n"
                    "pub struct Wide { a: u8 }"),
        fields=(FieldRecord(name="a", ty="u8"),))
    out = format_type_for_prompt(rec)
    assert "#[derive( Clone, Debug, )]" in out
    assert "#[repr(C)]" in out
    # A field attribute inside the body is never hoisted.
    rec2 = TypeRecord(
        kind="struct", name="Inner", generics="",
        definition="pub struct Inner {\n    #[repr(C)]\n    a: u8,\n}",
        fields=(FieldRecord(name="a", ty="u8"),))
    assert "#[" not in format_type_for_prompt(rec2)


# ---------------------------------------------------------------------------
# Authored `///` contract lines reach the fill prompt through this renderer
# ---------------------------------------------------------------------------

_CONTRACT = ("/// The TypeId stored for a message error is the message's own "
             "type, so a user's downcast::<&str>() finds it.")


def _documented_record(doc: str = _CONTRACT) -> TypeRecord:
    return TypeRecord(
        kind="struct", name="MessageError", generics="<M>",
        definition=f"{doc}\n#[repr(transparent)]\npub struct MessageError<M>(pub M);",
        fields=(FieldRecord(name="0", ty="M", is_pub=True),))


def test_type_doc_line_renders_above_the_layout_attrs():
    rec = _documented_record()
    out = format_type_for_prompt(rec)
    lines = out.splitlines()
    assert lines[0] == _CONTRACT
    assert lines[1] == "#[repr(transparent)]"
    assert lines[2] == "struct MessageError<M>"


def test_pre_change_renderer_never_carried_the_doc_line():
    """Control: the renderer used to read `definition` for its attributes only,
    so a documented record rendered exactly as the same record with the doc
    line stripped. That stripped rendering is reproduced here — it carries no
    `///` — and the documented rendering is that text plus the doc line, so
    the doc line is the ONLY thing this change adds."""
    rec = _documented_record()
    bare = format_type_for_prompt(
        replace(rec, definition=rec.definition.split("\n", 1)[1]))
    assert "///" not in bare
    assert bare.startswith("#[repr(transparent)]\nstruct MessageError<M>")
    assert format_type_for_prompt(rec) == _CONTRACT + "\n" + bare


def test_type_doc_lines_are_bounded_and_only_the_leading_block_counts():
    long = "/// " + "x" * 300
    rec = TypeRecord(
        kind="struct", name="Chatty",
        definition=("//! inner doc of the module, not of the item\n"
                    "// a plain comment\n"
                    "/// one\n/// two\n" + long + "\n/// four\n/// five\n"
                    "#[derive(Debug)]\n"
                    "/// not leading — after an attribute\n"
                    "pub struct Chatty { a: u8 }"),
        fields=(FieldRecord(name="a", ty="u8"),))
    out = format_type_for_prompt(rec)
    lines = out.splitlines()
    assert lines[0] == "/// one" and lines[1] == "/// two"
    assert lines[2].startswith("/// " + "x" * 190) and lines[2].endswith("…")
    assert len(lines[2]) <= len("/// ") + 200
    assert lines[3] == "#[derive(Debug)]"
    assert lines[4] == "struct Chatty"
    assert "four" not in out and "five" not in out
    assert "inner doc" not in out and "plain comment" not in out
    assert "not leading" not in out


def test_type_doc_truncation_marks_the_last_kept_line():
    rec = TypeRecord(kind="struct", name="T",
                     definition="/// a\n/// b\n/// c\n/// d\npub struct T;")
    out = format_type_for_prompt(rec)
    assert out.splitlines()[:3] == ["/// a", "/// b", "/// c …"]
    assert "/// d" not in out
