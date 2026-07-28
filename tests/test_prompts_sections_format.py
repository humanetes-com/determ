"""Unit tests for the pure prompt formatters in pipeline_core.prompts.sections:
format_type_for_prompt (TypeRecord → Rust-like def) and
format_impl_block_for_prompt (ImplBlock → target-form member listing).
"""
from __future__ import annotations

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
    # debt #85: `pub(crate) fn helper(...)` is NOT part of the crate's
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


from pipeline_core.prompts.sections import RUST_GOTCHAS, gotchas_for_error  # noqa: E402


def test_gotcha_e0034_ambiguous_method_fires_with_fq_examples():
    # E0034 → fully-qualified disambiguation, shown as WRONG→RIGHT.
    out = gotchas_for_error("error[E0034]: multiple applicable items in scope")
    assert "ambiguous_method_e0034" in {g["id"] for g in RUST_GOTCHAS
                                        if any(t in "E0034 multiple applicable items"
                                               for t in g["triggers"])}
    assert "TraitA::len(&value)" in out and "E0034" in out


def test_gotcha_e0599_invented_associated_ctor_fires():
    # E0599 `no function or associated item` → struct literal, NOT an
    # invented `T::new`.
    err = ("error[E0599]: no function or associated item named `new` found for "
           "struct `ContextError<C, E>`")
    out = gotchas_for_error(err)
    assert "invented_associated_ctor" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "no `new` on the struct" in out


def test_gotcha_field_through_type_path_fires():
    # E0599 whose named item IS a declared field → instance access, not a
    # struct literal and not a trait impl. Distinct remedy from
    # invented_associated_ctor, which fires on the same wording.
    err = ("error[E0599]: no function or associated item named `bytes` found "
           "for struct `Buf` in the current scope\n"
           "   = note: the following trait defines an item `bytes`, perhaps "
           "you need to implement it:\n"
           "           candidate #1: `std::io::Read`")
    out = gotchas_for_error(err)
    assert "field_through_type_path" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "`T::field` never" in out
    assert out.count("WRONG") >= 2   # ≥2 few-shot pairs
    # the trait-note trap must be named explicitly — it is the misleading part
    assert "coincidence" in out


def test_gotcha_invented_field_frozen_layout_fires():
    # E0609 `no field` → use the frozen layout in TYPES_IN_SCOPE, never
    # invent `.inner`/`.0`. Two WRONG→RIGHT pairs, generic placeholder names.
    err = "error[E0609]: no field `inner` on type `Wrap`"
    out = gotchas_for_error(err)
    assert "invented_field_frozen_layout" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "WRONG" in out and "RIGHT" in out
    assert out.count("WRONG") >= 2   # ≥2 few-shot pairs


def test_gotcha_ids_are_unique():
    ids = [g["id"] for g in RUST_GOTCHAS]
    assert len(ids) == len(set(ids))


def test_gotcha_e0424_self_confusion_fires():
    # M10 (plan_2): confirmed via starvation_miner on run 495747 — a trait
    # method's `self` receiver bled into a companion free fn with a
    # differently-named parameter, firing E0424 12+ times identically.
    err = ("error[E0424]: expected value, found module `self`\n"
           "   --> src/lib.rs:670:11\n"
           "    |\n"
           "669 |     pub fn not(cond: impl Bool) -> bool {\n"
           "    |            --- this function can't have a `self` parameter")
    out = gotchas_for_error(err)
    assert "self_is_not_a_free_fn_parameter" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "WRONG" in out and "RIGHT" in out
    assert out.count("WRONG") >= 2


def test_gotcha_std_type_without_default_fires():
    # Live (anyhow stable, src/error.rs::construct): three retro rounds spent
    # cycling Backtrace::default() → disabled() → capture() → force_capture()
    # on this exact bound. The class is "this std type has no Default at all",
    # so no other Default-shaped call helps — the gotcha must say so.
    err = ("error[E0277]: the trait bound `Backtrace: Default` is not satisfied\n"
           "   --> src/error.rs:296:44\n"
           "    |\n"
           "296 |         let backtrace = backtrace.unwrap_or_default();\n"
           "    |                                   ^^^^^^^^^^^^^^^^^^")
    out = gotchas_for_error(err)
    assert "std_type_without_default" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "WRONG" in out and "RIGHT" in out
    assert out.count("WRONG") >= 2
    # Names real stable constructors, not another Default-shaped guess.
    assert "Backtrace::capture()" in out and "Instant::now()" in out


def test_gotcha_e0401_self_from_outer_item_fires():
    # Live (anyhow nightly, impl ErrorImpl, run 2749): the model wrote a NESTED
    # `fn provide` that used `Self` in its signature and `Self::error` in its
    # body — E0401 recurred identically across attempts until the fill
    # exhausted, and the nested-fn/Self class had no gotcha of its own.
    err = ("error[E0401]: can't use `Self` from outer item\n"
           "   --> src/error.rs:755:35\n"
           "    |\n"
           "755 | fn provide<'a>(this: Ref<'a, Self>, request: &mut Request<'a>) {\n"
           "    |    -------                   ^^^^ use of `Self` from outer item\n"
           "    |    `Self` used in this inner function")
    out = gotchas_for_error(err)
    assert "self_from_outer_item_nested_fn" in {
        g["id"] for g in RUST_GOTCHAS if any(t in err for t in g["triggers"])}
    assert "WRONG" in out and "RIGHT" in out
    assert out.count("WRONG") >= 2
    # Names both fixes: concrete type AND hoisting to a sibling associated fn.
    assert "associated fn" in out
