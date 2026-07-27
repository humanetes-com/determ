"""Pins for the F11 scaffold rewrite (authored layout → skeleton)."""
from __future__ import annotations

from pipeline_core.domain.scaffold_rewrite import (
    DefReplacement,
    find_definition_span,
    replace_definition,
    replace_surface_definitions,
)


def test_whitespace_tolerant_span():
    skeleton = "mod ptr {\n    struct Own<T> {\n        ptr: NonNull<T>,\n    }\n}\n"
    enumerated = "struct Own<T> { ptr: NonNull<T>, }"
    span = find_definition_span(skeleton, enumerated)
    assert span is not None
    start, end = span
    assert skeleton[start:end].startswith("struct Own<T>")
    assert skeleton[start:end].rstrip().endswith("}")


def test_replace_swaps_authored_definition():
    skeleton = "struct ErrorImpl {\n    vtable: &'static ErrorVTable,\n}\n"
    old = "struct ErrorImpl { vtable: &'static ErrorVTable, }"
    new = "struct ErrorImpl { inner: Box<dyn StdError + Send + Sync> }"
    res = replace_definition(skeleton, old, new)
    assert res.replaced
    assert "Box<dyn StdError" in res.text
    assert "vtable" not in res.text


def test_missing_definition_is_no_op_flagged():
    res = replace_definition("struct A;", "struct Z {}", "struct Z2 {}")
    assert not res.replaced and res.text == "struct A;"


def test_empty_definition_not_matched():
    assert find_definition_span("anything", "   ") is None


# --- whole-fn expansion (fn_name replacements) ---------------------------

def test_fn_replacement_swaps_whole_item_not_just_signature():
    # Enumerated fn defs are bare signatures; the skeleton item carries
    # `pub(crate) unsafe` and a stub body. Sig-span-only replacement left
    # `pub(crate) pub(crate) … unsafe unsafe fn` and an orphaned `{ … }`.
    skeleton = ("pub(crate) unsafe fn object_drop<E>(e: Own<ErrorImpl>) {\n"
                "    unimplemented!()\n"
                "}\n")
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "fn object_drop<E>(e: Own<ErrorImpl>)",
        "unsafe fn object_drop<E>(e: Own<ErrorImpl>) { unimplemented!() }",
        fn_name="object_drop")])
    assert res.replaced
    assert res.text == (
        "unsafe fn object_drop<E>(e: Own<ErrorImpl>) { unimplemented!() }\n")


def test_fn_replacement_skips_same_name_frozen_public_twin():
    # error.rs `backtrace`: a frozen `pub fn` twin precedes the private item.
    # Its header never contains the enumerated private signature, so the
    # private pair must bind the second occurrence untouched-first-one.
    skeleton = ("pub fn backtrace(&self) -> &Backtrace { unimplemented!() }\n"
                "fn backtrace(&self) -> Option<&Backtrace> {\n"
                "    unimplemented!()\n"
                "}\n")
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "fn backtrace(&self) -> Option<&Backtrace>",
        "fn backtrace(&self) -> Option<&Backtrace> { None }",
        fn_name="backtrace")])
    assert res.replaced
    assert "pub fn backtrace(&self) -> &Backtrace { unimplemented!() }" in res.text
    assert "fn backtrace(&self) -> Option<&Backtrace> { None }" in res.text
    assert "Option<&Backtrace> {\n" not in res.text


def test_fn_replacement_identical_dup_sigs_bind_successive_items():
    skeleton = ("impl A for X {\n"
                "    fn source(&self) -> Opt { unimplemented!() }\n"
                "}\n"
                "impl A for Y {\n"
                "    fn source(&self) -> Opt { unimplemented!() }\n"
                "}\n")
    sig = "fn source(&self) -> Opt"
    res = replace_surface_definitions(skeleton, [
        DefReplacement(sig, "fn source(&self) -> Opt { x() }", fn_name="source"),
        DefReplacement(sig, "fn source(&self) -> Opt { y() }", fn_name="source"),
    ])
    assert res.replaced
    assert res.text.index("{ x() }") < res.text.index("{ y() }")
    assert "unimplemented" not in res.text


def test_fn_replacement_ignores_signature_text_in_comment():
    skeleton = ("// like fn erase(&self) -> u8 does\n"
                "fn erase(&self) -> u8 { unimplemented!() }\n")
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "fn erase(&self) -> u8", "fn erase(&self) -> u8 { 0 }",
        fn_name="erase")])
    assert res.replaced
    assert res.text == ("// like fn erase(&self) -> u8 does\n"
                        "fn erase(&self) -> u8 { 0 }\n")


def test_fn_replacement_consumes_the_old_outer_attributes():
    # Observed live on `ErrorImpl::backtrace`: the span started at `pub(crate)`,
    # so the skeleton's `#[cfg]` survived above a replacement carrying its own.
    # The two predicates AND together and a copy accumulates per re-splice.
    skeleton = ("impl ErrorImpl {\n"
                "    #[cfg(feature = \"std\")]\n"
                "    #[cold]\n"
                "    pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace "
                "{ unimplemented!() }\n"
                "}\n")
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace",
        "#[cfg(feature = \"std\")]\n"
        "pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace "
        "{ &this.deref().backtrace }",
        fn_name="backtrace")])
    assert res.replaced
    assert res.text.count("#[cfg(feature = \"std\")]") == 1
    assert "#[cold]" not in res.text          # dropped by the new item, not kept
    assert "unimplemented" not in res.text


def test_fn_replacement_is_idempotent_across_repeated_splices():
    # Each retrigger attempt re-splices the same item; attribute copies must not
    # accumulate, or the item drifts further from the authored layout per attempt.
    skeleton = ("#[cfg(any(feature = \"std\", not(anyhow_no_core_error)))]\n"
                "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E "
                "{ unimplemented!() }\n")
    new = ("#[cfg(any(feature = \"std\", not(anyhow_no_core_error)))]\n"
           "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E "
           "{ &mut *this }\n")
    text = skeleton
    for _ in range(3):
        res = replace_surface_definitions(text, [DefReplacement(
            "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E",
            new.rstrip("\n"), fn_name="error_mut")])
        assert res.replaced
        text = res.text
    assert text.count("#[cfg(any(feature = \"std\", not(anyhow_no_core_error)))]") == 1


def test_fn_replacement_attribute_span_leaves_the_public_twin_intact():
    # The attribute walk must not run past the target into the PRECEDING item's
    # body: an over-long span would swallow the frozen public API above it.
    skeleton = ("impl Error {\n"
                "    #[cfg(feature = \"std\")]\n"
                "    pub fn backtrace(&self) -> &Backtrace { real() }\n"
                "}\n"
                "impl ErrorImpl {\n"
                "    #[cfg(feature = \"std\")]\n"
                "    pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace "
                "{ unimplemented!() }\n"
                "}\n")
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace",
        "pub(crate) unsafe fn backtrace(this: Ref<Self>) -> &Backtrace { x() }",
        fn_name="backtrace")])
    assert res.replaced
    assert "pub fn backtrace(&self) -> &Backtrace { real() }" in res.text
    assert res.text.count("#[cfg(feature = \"std\")]") == 1  # public twin's kept
    assert "unimplemented" not in res.text


def test_fn_replacement_without_enclosing_fn_rejects_batch():
    # A trait-decl signature with no body has no fn item to expand into.
    skeleton = "trait T { fn source(&self) -> Opt; }\n"
    res = replace_surface_definitions(skeleton, [DefReplacement(
        "fn source(&self) -> Opt", "fn source(&self) -> Opt { None }",
        fn_name="source")])
    assert not res.replaced and res.text == skeleton
