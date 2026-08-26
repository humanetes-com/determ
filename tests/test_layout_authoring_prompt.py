"""Pins for the F11 layout-authoring prompt builder."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    OutcomeKind,
    StaticAssertKind,
)
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.prompts.layout_authoring import (
    _DOC_CONTRACT_RULE,
    CRASH_GUIDANCE,
    _fmt_outcome,
    _may_note,
    build_layout_authoring_prompt,
)


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


# ---------------------------------------------------------------------------
# Behaviours channel (the runtime MUSTs the pins alone never carried)
# ---------------------------------------------------------------------------

# Golden of the PRE-CHANGE builder: exactly `_control_prompt()`'s inputs, run
# through `build_layout_authoring_prompt` on 2026-08-26 BEFORE the `behaviours`
# parameter and `_DOC_CONTRACT_RULE` existed (`PYTHONPATH=. python` over the
# same call, `len(p)` and `hashlib.sha256(p.encode()).hexdigest()`).
_PRE_CHANGE_LEN = 7678
_PRE_CHANGE_SHA256 = "d6464c06f07e4e5dce91d9643aa25ee0f0d61ff07abec1a9c6fabecd1ab49718"


def _control_prompt(**overrides):
    items = (
        PrivateSurfaceItem(SurfaceItemKind.PUB_FIELDS, "Error", "src/lib.rs",
                           "pub struct Error { inner: Own<ErrorImpl> }"),
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
                           "struct ErrorImpl { .. }"),
        PrivateSurfaceItem(SurfaceItemKind.FN_SIG, "vtable", "src/error.rs",
                           "fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable"),
    )
    pin = BehaviourSpec(
        "p", "", "size_of::<Error>()", ObservableOutcome.static_assertion(
            StaticAssertKind.SIZE, "size_of::<Error>() == size_of::<usize>()"),
        subkind=BehaviourSubKind.OUTCOME)
    kwargs = {
        "module_name": "anyhow", "cfg_cluster": "stable", "items": items,
        "public_surface": "pub struct Error { /* private fields */ }",
        "layout_pins": [pin], "symbol_inventory": "Box, NonNull, StdError",
        "extra_guidance": CRASH_GUIDANCE,
        "authored_scheme": "struct Own<T> { ptr: Box<T> }",
        "item_uids": ("Error", "ErrorImpl", "vtable"),
        "item_anchors": {"ErrorImpl": "Error", "vtable": "Error"},
    }
    kwargs.update(overrides)
    return build_layout_authoring_prompt(**kwargs)


def _identity_spec(bid="b-identity"):
    return BehaviourSpec(
        bid, given='let e = Error::msg("x");', when="e.is::<&str>()",
        then=ObservableOutcome.downcast_ok(True),
        exercises=frozenset({"src/error.rs::msg::Error"}),
        subkind=BehaviourSubKind.OUTCOME, source_ref="tests/test_downcast.rs:12")


def _display_spec(bid="b-display", text="x"):
    return BehaviourSpec(
        bid, given='let e = Error::msg("x");', when="e.to_string()",
        then=ObservableOutcome.display(text), subkind=BehaviourSubKind.OUTCOME)


def test_no_behaviours_adds_nothing_but_the_doc_rule_to_the_pre_change_prompt():
    """The claim: with `behaviours=()` the behaviours channel contributes ZERO
    bytes — the section, its header and the MAY sentence are all absent — so
    the prompt is what it was before the channel existed, plus exactly the
    always-rendered `_DOC_CONTRACT_RULE` block (deliberate: the pins alone
    justify a contract line, see the rule's comment).

    Checked against a golden captured from the pre-change code, not against
    the current code: `_PRE_CHANGE_SHA256` is the hash of this very call on
    the builder as it stood before this batch. Removing the doc-rule block
    (and its join newline) from today's output must reproduce that hash
    byte for byte. Negative control: one behaviour changes the hash."""
    p = _control_prompt()
    assert _DOC_CONTRACT_RULE in p
    assert "BEHAVIOURS THIS REPRESENTATION" not in p
    assert _may_note() not in p
    stripped = p.replace(_DOC_CONTRACT_RULE + "\n", "", 1)
    assert len(stripped) == _PRE_CHANGE_LEN
    assert hashlib.sha256(stripped.encode()).hexdigest() == _PRE_CHANGE_SHA256
    with_one = _control_prompt(behaviours=[_identity_spec()])
    with_one = with_one.replace(_DOC_CONTRACT_RULE + "\n", "", 1)
    assert hashlib.sha256(with_one.encode()).hexdigest() != _PRE_CHANGE_SHA256


def test_behaviours_section_sits_after_the_pins_and_groups_identity_first():
    p = _control_prompt(behaviours=[_display_spec(), _identity_spec()])
    header = "=== BEHAVIOURS THIS REPRESENTATION MUST MAKE EXPRESSIBLE (MUST) ==="
    assert header in p
    assert (p.index("=== LAYOUT-PINNING TESTS") < p.index(header)
            < p.index("=== IN-SCOPE SYMBOL INVENTORY ==="))
    sec = p[p.index(header):p.index("=== IN-SCOPE SYMBOL INVENTORY ===")]
    # identity before exact-format even though the input listed them reversed.
    assert sec.index("IDENTITY (") < sec.index("EXACT_FORMAT (")
    assert "- when `e.is::<&str>()` then the downcast is Some (true)" in sec
    assert '- when `e.to_string()` then to_string() == "x"' in sec
    # ...and the section closes on the MAY sentence.
    assert sec.rstrip().endswith(_may_note())
    assert "(MAY); every behaviour listed is a MUST." in _may_note()


def test_type_identity_outcome_names_the_payload_typeid_rule():
    """`TYPE_IDENTITY` (value = the type literal, `ok` = whether `is::<T>()`
    holds) is being added to `OutcomeKind` beside this change; the renderer
    keys on the kind's NAME so it renders the moment the member exists. Until
    then a stand-in with the same shape exercises the same branch."""
    then = SimpleNamespace(kind=SimpleNamespace(name="TYPE_IDENTITY"),
                           value="&str", ok=True, chain=(), static_expr="")
    line = _fmt_outcome(then)
    assert line.startswith("`is::<&str>()` / `downcast::<&str>()` on that value is true")
    assert "the TypeId a user downcasts to is the payload's type, never a wrapper's" in line
    spec = BehaviourSpec("t", "", 'Error::msg("x").is::<&str>()', then=then,
                         subkind=BehaviourSubKind.OUTCOME)
    p = _control_prompt(behaviours=[spec])
    assert "IDENTITY (" in p and "`is::<&str>()` / `downcast::<&str>()`" in p


@pytest.mark.skipif(not hasattr(OutcomeKind, "TYPE_IDENTITY"),
                    reason="OutcomeKind.TYPE_IDENTITY not yet defined")
def test_real_type_identity_member_renders_the_same_line():
    then = ObservableOutcome(OutcomeKind.TYPE_IDENTITY, value="&str", ok=True)
    assert _fmt_outcome(then).startswith("`is::<&str>()` / `downcast::<&str>()` on that value is true")


def test_multi_line_values_are_fenced_and_every_kind_renders_its_exact_value():
    chain = BehaviourSpec(
        "c", "", "e.chain()", then=ObservableOutcome.chain_seq(("outer", "inner")),
        subkind=BehaviourSubKind.OUTCOME)
    debug = BehaviourSpec(
        "d", "", "format!(\"{e:?}\")", then=ObservableOutcome.debug("Outer\n\nCaused by:\n    inner"),
        subkind=BehaviourSubKind.OUTCOME)
    panic = BehaviourSpec(
        "p", "", "boom()", then=ObservableOutcome.panic("boom"),
        subkind=BehaviourSubKind.OUTCOME)
    usage = BehaviourSpec(
        "u", "let e = Error::msg(1);", "assert!(e.is::<i32>());\nassert!(!e.is::<&str>());",
        then=None, subkind=BehaviourSubKind.ASSERTION)
    p = _control_prompt(behaviours=[chain, debug, panic, usage])
    assert '- when `e.chain()` then chain() yields, in order: "outer", "inner"' in p
    assert 'format!("{:?}") ==\n  ```\n  Outer\n  \n  Caused by:\n      inner\n  ```' in p
    assert '- when `boom()` then panics with "boom"' in p
    # a multi-line `when` is fenced, and an assertion-only spec says so.
    assert ("- when:\n  ```\n  assert!(e.is::<i32>());\n  assert!(!e.is::<&str>());\n  ```\n"
            "  then the snippet's own assertions hold") in p


def test_behaviours_are_capped_by_count_and_report_the_rest():
    specs = [_display_spec(f"b{i}", text=f"v{i}") for i in range(45)]
    p = _control_prompt(behaviours=specs)
    sec = p[p.index("=== BEHAVIOURS"):p.index("=== IN-SCOPE SYMBOL INVENTORY ===")]
    assert sec.count("- when ") == 40
    assert "(+5 more)" in sec
    assert sec.rstrip().endswith(_may_note())


def test_behaviours_are_capped_by_chars_and_report_the_rest():
    specs = [_display_spec(f"b{i}", text="x" * 1500) for i in range(10)]
    p = _control_prompt(behaviours=specs)
    sec = p[p.index("=== BEHAVIOURS"):p.index("=== IN-SCOPE SYMBOL INVENTORY ===")]
    shown = sec.count("- when ")
    assert 0 < shown < 10
    assert f"(+{10 - shown} more)" in sec
    assert len(sec) < 6000 + 1500 + len(_may_note()) + 200


def test_doc_contract_rule_is_always_rendered_and_names_the_behaviours_section():
    """Every authored item opens with `///` lines stating its contract, written
    from THIS prompt's behaviours/pins, never copied from a library's docs.
    Rendered even without behaviours: the pins alone justify the line."""
    for p in (_prompt(), _control_prompt(), _control_prompt(behaviours=[_identity_spec()])):
        assert "EVERY ITEM OPENS WITH ITS CONTRACT, AS `///` DOC LINES:" in p
        assert "never copy any library's documentation" in p
        assert "At most\ntwo lines" in p
        assert ("/// The TypeId stored for a message error is the message's own "
                "type, so a user's downcast::<&str>() finds it.") in p
        # a fn example too — kind="fn" blocks are bare items and may open with docs.
        assert "fn payload_ptr(&self) -> *const () { unimplemented!() }" in p


def test_doc_contract_rule_uses_placeholder_names_only():
    for leaked in ("anyhow", "ErrorImpl", "Own<", "MessageError"):
        assert leaked not in _DOC_CONTRACT_RULE
