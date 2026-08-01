"""Pins for the F11 scaffold rewrite (authored layout → skeleton)."""
from __future__ import annotations

from pipeline_core.domain.rust_scan import duplicate_attribute_runs
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


def test_no_attribute_is_ever_repeated_within_a_run_after_a_re_splice():
    """The invariant behind the specific counts above, asserted directly.

    A splice that re-attaches an item's attributes instead of replacing them
    accumulates them silently: rustc treats repeated `cfg`/`cold` as
    idempotent, so no build ever fails and the only symptom is a count nobody
    re-measures. On `.skeleton-filled/stable/src/error.rs` it reached 17
    duplicated runs, several of them x3, before the 2026-07-27 repair expanded
    the splice span back over the outer attributes.

    Stated as "no attribute occurs twice in one run" rather than as a count of
    one chosen attribute, so a regression on ANY attribute fails this.
    """
    skeleton = (
        '#[cfg(any(feature = "std", not(anyhow_no_core_error)))]\n'
        "#[cold]\n"
        "#[inline]\n"
        "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E "
        "{ unimplemented!() }\n"
    )
    new = (
        '#[cfg(any(feature = "std", not(anyhow_no_core_error)))]\n'
        "#[cold]\n"
        "#[inline]\n"
        "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E "
        "{ &mut *this }\n"
    )
    text = skeleton
    for _ in range(4):
        res = replace_surface_definitions(text, [DefReplacement(
            "pub(crate) unsafe fn error_mut(this: Mut<Self>) -> &mut dyn E",
            new.rstrip("\n"), fn_name="error_mut")])
        assert res.replaced
        text = res.text
    assert duplicate_attribute_runs(text) == []


def test_a_wrapped_attribute_is_compared_whole_after_a_re_splice():
    """`prettyplease` re-emits a long `#[allow(...)]` across several lines.

    A line-at-a-time guard sees the continuation lines as the end of the run
    and so cannot compare the two copies at all — it would report a clean file
    while the duplication accumulated. The check must join each attribute
    before comparing, which is what makes this pin meaningful on a tree the
    Rust side has re-emitted.
    """
    wrapped = (
        "#[allow(\n"
        "    clippy::needless_lifetimes,\n"
        "    clippy::wrong_self_convention,\n"
        ")]\n"
    )
    item = "pub(crate) fn provide(&self) -> u8 { unimplemented!() }\n"
    # Two copies of the SAME wrapped attribute — what a re-prepend produces.
    assert duplicate_attribute_runs(wrapped + wrapped + item) != []
    # One copy is clean, however many lines it spans.
    assert duplicate_attribute_runs(wrapped + item) == []

    res = replace_surface_definitions(wrapped + item, [DefReplacement(
        "pub(crate) fn provide(&self) -> u8",
        (wrapped + "pub(crate) fn provide(&self) -> u8 { 0 }").rstrip("\n"),
        fn_name="provide")])
    assert res.replaced
    assert duplicate_attribute_runs(res.text) == []


_ERROR_DECL = (
    "#[repr(transparent)]\n"
    "pub struct Error {\n"
    "    ptr: core::ptr::NonNull<ErrorImpl>,\n"
    "}\n"
)
_AUTO_TRAITS = (
    "unsafe impl Send for Error {}\n"
    "unsafe impl Sync for Error {}\n"
)


def test_reauthoring_a_definition_does_not_leave_its_old_trait_impls():
    """The live-tree incident of 2026-08-01, in miniature.

    A registry definition is not always one item: `Error` is a struct plus the
    two auto-trait impls written beside it. The span it replaces is located by
    the `struct Error` DECLARATION, so three items go into a hole one item
    wide. The previous install's copies stayed put, rustc reported E0119 twice
    on `src/lib.rs:277`, `nightly` fell from 93 passing tests to a tree that
    does not build, and no fill route could aim at the offending lines.
    """
    live = "pub use anyhow as format_err;\n" + _ERROR_DECL + "\n" + _AUTO_TRAITS
    new = (
        "#[repr(transparent)]\n"
        "pub struct Error(core::ptr::NonNull<crate::error::ErrorImpl>);\n"
        + _AUTO_TRAITS.rstrip("\n")
    )
    res = replace_surface_definitions(live, [DefReplacement(
        "#[repr(transparent)] pub struct Error { ptr: core::ptr::NonNull<ErrorImpl>, }",
        new)])
    assert res.replaced
    assert res.text.count("impl Send for Error") == 1
    assert res.text.count("impl Sync for Error") == 1
    assert "NonNull<crate::error::ErrorImpl>" in res.text


def test_a_trait_impl_inside_the_replaced_span_is_not_deleted_twice():
    """When the locate key already covers the companion impls, they go out with
    the span being replaced. Treating them as strays as well would delete the
    text the splice is standing on."""
    live = _ERROR_DECL + _AUTO_TRAITS + "struct After;\n"
    new = _ERROR_DECL.rstrip("\n") + "\n" + _AUTO_TRAITS.rstrip("\n")
    res = replace_surface_definitions(
        live, [DefReplacement((_ERROR_DECL + _AUTO_TRAITS).strip(), new)])
    assert res.replaced
    assert res.text.count("impl Send for Error") == 1
    assert res.text.rstrip().endswith("struct After;")


def test_an_unrelated_trait_impl_survives_the_splice():
    """Only impls the incoming text re-declares are strays. Anything else in
    the file is somebody's working code."""
    live = _ERROR_DECL + "impl Debug for Error {}\n" + _AUTO_TRAITS
    new = "pub struct Error(NonNull<ErrorImpl>);\n" + _AUTO_TRAITS.rstrip("\n")
    res = replace_surface_definitions(live, [DefReplacement(
        "#[repr(transparent)] pub struct Error { ptr: core::ptr::NonNull<ErrorImpl>, }",
        new)])
    assert res.replaced
    assert res.text.count("impl Debug for Error") == 1
    assert res.text.count("impl Send for Error") == 1


def test_a_fn_replacement_deletes_no_impl():
    """A fn item declares no trait impl, so a body splice must leave every impl
    in the file exactly where it was."""
    live = "impl Send for Error {}\nfn provide(&self) -> u8 { unimplemented!() }\n"
    res = replace_surface_definitions(live, [DefReplacement(
        "fn provide(&self) -> u8", "fn provide(&self) -> u8 { 0 }",
        fn_name="provide")])
    assert res.replaced
    assert "impl Send for Error {}" in res.text
