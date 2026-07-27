"""Pins for the F11 layout-authoring prompt builder."""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.prompts.layout_authoring import build_layout_authoring_prompt


def _prompt():
    items = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
                           "struct ErrorImpl { .. }"),
    )
    pin = BehaviourSpec(
        "p", "", "size_of::<Error>()", ObservableOutcome.static_assertion(
            StaticAssertKind.SIZE, "size_of::<Error>() == size_of::<usize>()"),
        subkind=BehaviourSubKind.OUTCOME)
    return build_layout_authoring_prompt(
        module_name="anyhow", cfg_cluster="stable", items=items,
        public_surface="pub struct Error { .. }",
        layout_pins=[pin], symbol_inventory="Box, NonNull, StdError")


def test_prompt_states_the_protocol_and_items():
    p = _prompt()
    assert "<LAYOUT_ITEM" in p
    assert "ErrorImpl" in p and "src/error.rs" in p
    # the layout pin is surfaced verbatim.
    assert "size_of::<Error>() == size_of::<usize>()" in p


def test_prompt_forbids_reproducing_original():
    p = _prompt()
    assert "invent your own scheme" in p
    assert "unsafe" in p  # steers toward safe representation
    # public surface is included and named frozen.
    assert "FROZEN PUBLIC SURFACE" in p and "pub struct Error" in p

def test_item_listing_withholds_the_frozen_definition():
    # The listing used to render `- {uid} ({kind}) in {file} — currently:
    # {first line of the frozen definition}`, which handed over the very design
    # the model is asked to invent (`#[repr(C)]`, `fn vtable(p:
    # NonNull<ErrorImpl>) -> &'static ErrorVTable`) — and the reply came back
    # shaped like it.
    items = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
                           "#[repr(C)]\nstruct ErrorImpl { vtable: &'static ErrorVTable }"),
        PrivateSurfaceItem(SurfaceItemKind.FN_SIG, "vtable", "src/error.rs",
                           "fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable"),
    )
    p = build_layout_authoring_prompt(
        module_name="anyhow", cfg_cluster="stable", items=items,
        public_surface="pub struct Error { .. }", layout_pins=[],
        symbol_inventory="Box, NonNull")
    assert "currently:" not in p
    assert "#[repr(C)]" not in p
    assert "NonNull<ErrorImpl>" not in p
    # the items are still listed, by uid + kind + file.
    assert "- ErrorImpl (struct) in src/error.rs" in p
    assert "- vtable (fn_sig) in src/error.rs" in p


def test_item_listing_disambiguates_by_the_public_type_it_anchors_to():
    items = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "Inner", "src/a.rs", "struct Inner;"),
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "Inner", "src/b.rs", "struct Inner;"),
    )
    p = build_layout_authoring_prompt(
        module_name="c", cfg_cluster="stable", items=items,
        public_surface="", layout_pins=[], symbol_inventory="",
        item_anchors={"Inner.1": "Alpha", "Inner.2": "Beta"})
    assert "- Inner.1 (struct) in src/a.rs — reached from the public `Alpha`" in p
    assert "- Inner.2 (struct) in src/b.rs — reached from the public `Beta`" in p


def test_protocol_forbids_wrapping_a_fn_in_an_impl_header():
    """A kind="fn" block splices in place INSIDE its existing impl, so a reply
    wrapping the fn in `impl Foo { … }` produces a nested impl — rustc's
    `implementation is not supported in `trait`s or `impl`s`. That error burned
    all 3 attempts on anyhow's 34-item component on nightly (and 2 of 3 on
    stable), so the protocol must show the mistake, not just describe the rule.
    """
    p = _prompt()
    assert "implementation is not supported in" in p
    assert "impl Inner { fn drop_inner(&mut self) { unimplemented!() } }" in p
    # ...and both diverse WRONG cases carry their RIGHT counterpart.
    assert "fn drop_inner(&mut self) { unimplemented!() }" in p
    assert "unsafe fn width(p: NonNull<Cell>) -> usize { unimplemented!() }" in p
    # Types are NOT bare — the carve-out must survive.
    assert 'kind="struct"' in p


def _pub_fields_prompt():
    items = (
        PrivateSurfaceItem(SurfaceItemKind.PUB_FIELDS, "Error", "src/lib.rs",
                           "pub struct Error { inner: Own<ErrorImpl> }"),
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
                           "struct ErrorImpl { .. }"),
    )
    return build_layout_authoring_prompt(
        module_name="anyhow", cfg_cluster="stable", items=items,
        public_surface="pub struct Error { /* private fields */ }",
        layout_pins=[], symbol_inventory="Box")


def test_pub_fields_item_states_its_contract_and_leaks_no_layout():
    p = _pub_fields_prompt()
    assert "Error (struct) in src/lib.rs — PUBLIC type" in p
    assert "design ONLY its fields" in p
    # the rule block with its WRONG→RIGHT examples rides along...
    assert "pub struct Handle<T: ?Sized> { inner: Box<T> }" in p
    # ...and the item's own current field is still nowhere in the prompt.
    assert "Own<ErrorImpl>" not in p


def test_pub_fields_rule_absent_when_no_such_item_is_listed():
    assert "PUBLIC STRUCTS ON THE LIST" not in _prompt()


def test_prompt_states_the_autotrait_obligation():
    """A private field type silently strips Send/Sync from the PUBLIC type that
    holds it; the break then surfaces as a test-binary compile error far from
    the layout that caused it, so the obligation must be stated where the
    layout is chosen."""
    p = _prompt()
    assert "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API" in p
    assert "cannot be sent between threads safely" in p
    # Concrete WRONG->RIGHT, not abstract advice: both escape hatches shown.
    assert "unsafe impl Send for Handle {}" in p
    assert "pub struct Handle { inner: Box<Repr> }" in p


def test_autotrait_rule_uses_placeholder_names_only():
    """Guidance must never leak crate-specific naming into the model's prompt."""
    from pipeline_core.prompts.layout_authoring import _AUTOTRAIT_RULE
    for leaked in ("anyhow", "ErrorImpl", "Own<"):
        assert leaked not in _AUTOTRAIT_RULE
