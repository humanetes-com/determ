"""Pins for the layout-pinning classifier (shared F10/F11 primitive)."""
from __future__ import annotations

from pipeline_core.domain.behaviour import OutcomeKind, StaticAssertKind
from pipeline_core.domain.layout_pins import (
    classify_layout_assertions,
    harvest_layout_pin_behaviours,
    is_layout_pinning,
    pinned_type_names,
)


def _kinds(src):
    return {o.static_kind for o in classify_layout_assertions(src)}


def test_size_assertion_detected():
    src = "assert_eq!(size_of::<Error>(), size_of::<usize>());"
    assert _kinds(src) == {StaticAssertKind.SIZE}
    assert is_layout_pinning(src)


def test_niche_detected_not_plain_size():
    # NPO: Option<Error> is the same size as Error — a niche pin, not a SIZE pin.
    src = "assert_eq!(size_of::<Result<(), Error>>(), size_of::<Option<Error>>());"
    assert StaticAssertKind.NICHE in _kinds(src)


def test_align_assertion_detected():
    src = "assert_eq!(align_of::<Error>(), align_of::<usize>());"
    assert _kinds(src) == {StaticAssertKind.ALIGN}


def test_autotrait_macro_detected():
    src = "assert_impl_all!(Error: Send, Sync, Unpin, 'static);"
    assert _kinds(src) == {StaticAssertKind.AUTOTRAITS}


def test_where_bound_autotrait_detected():
    src = "fn _assert_send<T: Send + Sync>() {}\n_assert_send::<Error>();"
    assert StaticAssertKind.AUTOTRAITS in _kinds(src)


def test_non_layout_test_is_not_pinning():
    src = 'let e = anyhow!("boom");\nassert_eq!(e.to_string(), "boom");'
    assert not is_layout_pinning(src)
    assert classify_layout_assertions(src) == ()


def test_dedup_repeated_assertions():
    src = ("assert_eq!(size_of::<Error>(), 8);\n"
           "assert_eq!(size_of::<Error>(), 8);")
    assert len(classify_layout_assertions(src)) == 1


# --- M12: harvest layout pins from a whole test file --------------------------

_REPR_TEST = """
#[test]
fn test_error_size() {
    assert_eq!(size_of::<Error>(), size_of::<usize>());
    assert_eq!(size_of::<Result<(), Error>>(), size_of::<Option<Error>>());
}
"""


def test_harvest_layout_pins_from_test_file():
    specs = harvest_layout_pin_behaviours(
        _REPR_TEST, source_file="tests/repr.rs", cfg_cluster="stable")
    assert specs, "repr test must yield layout-pin behaviours"
    for s in specs:
        assert s.then is not None
        assert s.then.kind is OutcomeKind.STATIC_ASSERTION
        assert s.cfg_cluster == "stable"
        assert s.source_ref == "tests/repr.rs"


def test_harvest_layout_pins_none_for_plain_test():
    assert harvest_layout_pin_behaviours(
        "#[test]\nfn t() { assert_eq!(1 + 1, 2); }",
        source_file="tests/math.rs") == []


def test_pinned_type_names_extracts_the_pinned_type():
    specs = tuple(harvest_layout_pin_behaviours(
        _REPR_TEST, source_file="tests/repr.rs"))
    assert "Error" in pinned_type_names(specs)


def test_ordinary_code_mentioning_an_autotrait_is_not_a_pin() -> None:
    # Live defect: any line with Send/Sync/Unpin AND a colon was harvested as a
    # layout pin, so eight ordinary lines from anyhow's tests reached the t1.5
    # authoring prompt as constraints the model was told its layout must satisfy.
    for code in (
        "let boxed_error: Box<dyn StdError + Send + Sync + 'static> = e.into();",
        "fn from_boxed(e: Box<dyn StdError + Send + Sync + 'static>) -> Self {",
        "let error = Box::<dyn StdError + Send + Sync>::from(\"oh no!\");",
        "    .downcast_ref::<Box<dyn StdError + Send + Sync>>()",
        "let r: &(dyn StdError + Send + Sync) = error.as_ref();",
    ):
        assert classify_layout_assertions(code) == (), code


def test_generic_helper_and_where_clause_forms_are_still_pins() -> None:
    for pin in (
        "fn assert_send<T: Send>() {}",
        "fn assert<E: Unpin + Send + Sync + 'static>() {}",
        "fn _assert() where Error: Send + Sync {}",
    ):
        got = classify_layout_assertions(pin)
        assert len(got) == 1 and got[0].static_kind is StaticAssertKind.AUTOTRAITS, pin


_ANYHOW_AUTOTRAIT_TEST = """
#[test]
fn test_send() {
    fn assert_send<T: Send>() {}
    assert_send::<Error>();
}

#[test]
fn test_sync() {
    fn assert_sync<T: Sync>() {}
    assert_sync::<Error>();
}
"""


def test_autotrait_pin_carries_the_type_it_is_applied_to() -> None:
    # Live defect: the pin was recorded as the helper's DECLARATION alone, so
    # its type names were the bounds (`Send`) and never the subject (`Error`).
    # Nothing routes a pin but those names — the t1.5 selector matches them
    # against a component's items and against the frozen-public names — so all
    # four of anyhow's autotrait pins selected into ZERO components on
    # 2026-08-06 while the two `size_of::<Error>()` pins in the same crate
    # routed correctly. t1.5 then authored `Error { inner: NonNull<ErrorImpl> }`,
    # which is neither Send nor Sync and satisfies both size pins.
    specs = tuple(harvest_layout_pin_behaviours(
        _ANYHOW_AUTOTRAIT_TEST, source_file="tests/test_autotrait.rs"))
    assert "Error" in pinned_type_names(specs)
    exprs = [s.then.static_expr for s in specs if s.then is not None]
    assert exprs == [
        "fn assert_send<T: Send>() {} assert_send::<Error>();",
        "fn assert_sync<T: Sync>() {} assert_sync::<Error>();",
    ]


def test_autotrait_pin_without_a_call_site_keeps_the_declaration() -> None:
    got = classify_layout_assertions("fn assert_send<T: Send>() {}")
    assert [o.static_expr for o in got] == ["fn assert_send<T: Send>() {}"]


def test_autotrait_subject_is_read_by_bracket_depth_and_whole_word() -> None:
    from pipeline_core.domain.layout_pins import autotrait_subject_types
    src = ("assert_eq!(size_of::<Error>(), 8);\n"
           "fn assert<E: Send>() {}\n"
           "assert::<Result<(), Error>>();\n")
    # `assert_eq` must not answer for `assert`, and a nested generic is ONE type
    assert autotrait_subject_types(src, "assert") == ["Result<(), Error>"]


def test_autotrait_pin_still_renders_its_bounds_with_the_call_attached() -> None:
    # The probe reads the bounds off the same expression the subject now shares.
    from pipeline_core.domain.layout_pins import autotrait_bound_list
    expr = classify_layout_assertions(_ANYHOW_AUTOTRAIT_TEST)[0].static_expr
    assert autotrait_bound_list(expr) == "Send"


def test_autotrait_bound_list_survives_nested_generics() -> None:
    from pipeline_core.domain.layout_pins import autotrait_bound_list
    assert autotrait_bound_list("fn a<T: AsRef<[u8]> + Send>() {}") == (
        "AsRef<[u8]> + Send")
    # two bounded params are ambiguous (which one is the pin?) — not a pin
    assert autotrait_bound_list("fn a<A: Send, B: Sync>() {}") is None
    assert autotrait_bound_list("fn a() {}") is None
