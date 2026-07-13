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
