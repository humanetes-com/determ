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
