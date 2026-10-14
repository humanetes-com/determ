"""Pins for reinstalling an authored layout into the live (filled) tree.

`.skeleton-filled/` is seeded from the skeleton once and owned thereafter, so a
layout re-authored mid-run lands in the skeleton alone. These pin that the drift
is detected from the store, repaired without touching a filled body, and reports
nothing at all when the tree is already correct.
"""
from __future__ import annotations

from pipeline_core.application.author_layout import (
    LayoutComponentRecord,
    LayoutOutcome,
)
from pipeline_core.application.reconcile_layout import plan_layout_reconciliation
from pipeline_core.domain.authored_layout import component_uids
from pipeline_core.domain.models import FieldRecord, FnRecord, TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface

_OLD = "pub struct Error { ptr: NonNull<ErrorImpl> }"
_NEW = ("pub struct Error { ptr: NonNull<ErrorImpl> }\n\n"
        "unsafe impl Send for Error {}\nunsafe impl Sync for Error {}")
_IMPL_OLD = "struct ErrorImpl { vtable: u8 }"


def _surface():
    return classify_private_surface([
        TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True,
                   definition=_OLD,
                   fields=[FieldRecord(name="ptr", ty="NonNull<ErrorImpl>")]),
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition=_IMPL_OLD),
    ], [], cfg_cluster="stable", author_private_fields=True)


def _live_types():
    return [
        TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True,
                   definition=_OLD),
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition=_IMPL_OLD),
    ]


def _record(*, error_def: str) -> LayoutComponentRecord:
    surface = _surface()
    comp = next(c for c in surface.components
                if any(i.name == "Error" for i in c))
    uids = component_uids(comp)
    defs = []
    for uid, it in zip(uids, comp, strict=True):
        defs.append((uid, error_def if it.name == "Error" else _IMPL_OLD))
    return LayoutComponentRecord(
        frozenset(i.name for i in comp), LayoutOutcome.AUTHORED, tuple(defs))


def _plan(*, error_def: str, live_text: str):
    return plan_layout_reconciliation(
        _surface(), [_record(error_def=error_def)], lambda rel: live_text)


def test_superseded_definition_is_planned_for_reinstall():
    plan, notes = _plan(error_def=_NEW, live_text=_OLD + "\n" + _IMPL_OLD)
    assert "lib.rs" in plan
    rep = plan["lib.rs"][0]
    assert rep.old == _OLD and "unsafe impl Send for Error {}" in rep.new
    assert any("superseded" in n for n in notes)


def test_tree_already_holding_the_authored_text_plans_nothing():
    # The normal case on every healthy iteration — this must cost nothing and
    # must never rewrite a file just because the store has a row for it.
    plan, _notes = _plan(error_def=_NEW, live_text=_NEW + "\n" + _IMPL_OLD)
    assert plan == {}


def test_unauthored_rows_are_ignored():
    surface = _surface()
    comp = next(iter(surface.components))
    for outcome in (LayoutOutcome.FROZEN, LayoutOutcome.GATED):
        rec = LayoutComponentRecord(
            frozenset(i.name for i in comp), outcome,
            tuple((u, _NEW) for u in component_uids(comp)))
        plan, _ = plan_layout_reconciliation(
            surface, [rec], lambda rel: _OLD)
        assert plan == {}, outcome


def test_fn_items_are_never_reinstalled():
    # A fn's stored key is a SIGNATURE, not a whole item; splicing it would
    # delete the filled body. Bodies belong to the fill loop.
    surface = classify_private_surface(
        [TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                    is_pub=False, definition=_IMPL_OLD)],
        [FnRecord(file="error.rs", fn_name="erase", sig_text="fn erase(self) -> u8",
                  body_text="unimplemented!()", has_unimplemented_body=True)],
        cfg_cluster="stable")
    comp = next(iter(surface.components))
    rec = LayoutComponentRecord(
        frozenset(i.name for i in comp), LayoutOutcome.AUTHORED,
        tuple((u, "fn erase(self) -> u16") for u in component_uids(comp)))
    plan, _ = plan_layout_reconciliation(
        surface, [rec], lambda rel: _IMPL_OLD)
    assert all(r.old != "fn erase(self) -> u8"
               for reps in plan.values() for r in reps)


# --- the locator: an extractor's rendering is NOT the bytes on disk -----------
_WRAPPED = """\
#[repr(C)]
pub(crate) struct ErrorVTable {
    object_ref: unsafe fn(Ref<ErrorImpl>) -> Ref<dyn StdError>,
}
pub struct After;
"""


def test_unreadable_live_file_is_reported_not_skipped_silently():
    # An empty plan means "healthy tree" AND "read nothing at all" — the second
    # is how a base-dir mistake hides: every item resolves to a missing path and
    # the sync reports a clean no-op forever.
    plan, notes = plan_layout_reconciliation(
        _surface(), [_record(error_def=_NEW)], lambda rel: None)
    assert plan == {}
    assert any("could not be read" in n for n in notes)


def test_locator_takes_visibility_and_attributes_with_the_item():
    # Stopping at the keyword would splice `pub(crate) pub(crate) struct` and
    # leave the old `#[repr(C)]` above the new one.
    from pipeline_core.domain.rust_scan import locate_top_level_item
    span = locate_top_level_item(_WRAPPED, "struct", "ErrorVTable")
    assert span is not None
    seg = _WRAPPED[span[0]:span[1]]
    assert seg.startswith("#[repr(C)]\npub(crate) struct ErrorVTable")
    assert seg.endswith("}")
    assert "After" not in seg


def test_locator_matches_source_that_syns_rendering_would_miss():
    # The concrete defect: syn re-wraps a long fn-pointer field and adds a
    # trailing comma inside the wrapped argument list, so the rendered text is
    # not a whitespace-tolerant match for the source.
    from pipeline_core.domain.rust_scan import locate_top_level_item
    from pipeline_core.domain.scaffold_rewrite import find_definition_span
    rendered = ("pub(crate) struct ErrorVTable {\n    object_ref: unsafe fn(\n"
                "        Ref<ErrorImpl>,\n    ) -> Ref<dyn StdError>,\n}")
    assert find_definition_span(_WRAPPED, rendered) is None   # the old approach
    assert locate_top_level_item(_WRAPPED, "struct", "ErrorVTable") is not None


def test_locator_stops_at_a_semicolon_declaration():
    from pipeline_core.domain.rust_scan import locate_top_level_item
    text = "pub struct Unit;\npub struct Next { a: u8 }\n"
    span = locate_top_level_item(text, "struct", "Unit")
    assert text[span[0]:span[1]] == "pub struct Unit;"
    tuple_text = "struct Wrap(u8);\nstruct Other {}\n"
    sp2 = locate_top_level_item(tuple_text, "struct", "Wrap")
    assert tuple_text[sp2[0]:sp2[1]] == "struct Wrap(u8);"


def test_locator_does_not_end_an_item_on_an_array_length_semicolon():
    # `[u8; 16]` puts a `;` inside the declaration's own header. Ending there
    # cuts the span mid-type, and splicing a replacement over it leaves
    # ` 16]);` behind — a file that no longer parses, written into the tree
    # cargo builds.
    from pipeline_core.domain.rust_scan import locate_top_level_item
    text = "struct Buf([u8; 16]);\npub struct Next { a: u8 }\n"
    span = locate_top_level_item(text, "struct", "Buf")
    assert text[span[0]:span[1]] == "struct Buf([u8; 16]);"
    alias = "type Repr = [u8; 4];\nstruct After;\n"
    sp2 = locate_top_level_item(alias, "type", "Repr")
    assert alias[sp2[0]:sp2[1]] == "type Repr = [u8; 4];"
    braced = "struct Owned { buf: [u8; 8] }\nstruct After;\n"
    sp3 = locate_top_level_item(braced, "struct", "Owned")
    assert braced[sp3[0]:sp3[1]] == "struct Owned { buf: [u8; 8] }"


def test_locator_ignores_nested_and_commented_occurrences():
    from pipeline_core.domain.rust_scan import locate_top_level_item
    text = ("fn f() {\n    struct Inner { a: u8 }\n}\n"
            "// struct Target { commented: u8 }\n"
            "pub struct Target { real: u8 }\n")
    span = locate_top_level_item(text, "struct", "Target")
    assert text[span[0]:span[1]] == "pub struct Target { real: u8 }"
    assert locate_top_level_item(text, "struct", "Inner") is None   # depth > 0
    assert locate_top_level_item(text, "struct", "Absent") is None


# `mod ext { pub trait StdError … }` — a real `anyhow` surface item, and the
# whole class the depth-0-only locator reported as absent. Absent is what a
# caller acts on, so the propagation failed MECHANICALLY, refunded its attempt
# and re-fired on the identical bug until the mechanical cap blocked the
# component. Live: `StdError could not be located in context.rs`.
_MOD_NESTED = ("mod ext {\n    use super::*;\n"
               "    pub trait StdError {\n"
               "        fn ext_context<C>(self, c: C) -> Error;\n    }\n}\n")


def test_declaration_locator_reaches_inside_an_inline_mod():
    from pipeline_core.domain.rust_scan import (
        locate_item_declaration,
        locate_top_level_item,
    )
    assert locate_top_level_item(_MOD_NESTED, "trait", "StdError") is None
    span = locate_item_declaration(_MOD_NESTED, "trait", "StdError")
    assert span is not None
    seg = _MOD_NESTED[span[0]:span[1]]
    assert seg.startswith("pub trait StdError {")
    assert seg.endswith("}")
    assert "mod ext" not in seg          # the item, not its module


def test_declaration_locator_prefers_the_top_level_declaration():
    # Additive by construction: anything resolving today must keep resolving to
    # the same span, so a depth-0 hit wins outright and never ties with a
    # nested namesake.
    from pipeline_core.domain.rust_scan import locate_item_declaration
    text = ("mod inner {\n    pub struct Dup { nested: u8 }\n}\n"
            "pub struct Dup { top: u8 }\n")
    span = locate_item_declaration(text, "struct", "Dup")
    assert text[span[0]:span[1]] == "pub struct Dup { top: u8 }"


def test_declaration_locator_refuses_an_ambiguous_nested_name():
    # Two same-named items in sibling modules are two different types. Slicing
    # whichever comes first rewrites a declaration the caller never named — and
    # a "could not locate" is recoverable where a wrong splice is not.
    from pipeline_core.domain.rust_scan import locate_item_declaration
    text = ("mod a {\n    pub struct Dup { x: u8 }\n}\n"
            "mod b {\n    pub struct Dup { y: u8 }\n}\n")
    assert locate_item_declaration(text, "struct", "Dup") is None


def test_live_definition_slices_a_mod_nested_surface_item():
    # End-to-end over the real caller: `live_definition` is what both the
    # startup reconciliation and the retrigger's live-tree plan ask, and what
    # returned None for `mod ext`'s items.
    from pipeline_core.application.reconcile_layout import live_definition
    surface = classify_private_surface([
        TypeRecord(file="context.rs", kind="trait", name="StdError",
                   is_pub=False, definition="pub trait StdError {}"),
    ], [], cfg_cluster="stable", author_private_fields=True)
    item = next(i for i in surface.replaceable if i.name == "StdError")
    sliced = live_definition(_MOD_NESTED, item)
    assert sliced is not None
    assert sliced.startswith("pub trait StdError {")
    assert _MOD_NESTED.count(sliced) == 1   # a unique splice target


# --- the crate-wide registry outranks a per-cluster record -------------------
#
# A cluster that authored an item the crate had already fixed persists a SECOND
# definition of it. Live on `anyhow`: `stable` and `nightly` both recorded
# AUTHORED for the same 35-item component, every uid present in the registry,
# and 32 definitions differed — `nightly`'s `Error` lacking the two
# `unsafe impl`s, which is six `E0277 cannot be sent between threads` in the
# oracle's own tests and unreachable by any fill.

def _registry(**defs: str):
    from pipeline_core.domain.authored_layout import AuthoredItem
    return {uid: AuthoredItem(name=uid, kind="struct", definition=d)
            for uid, d in defs.items()}


def test_registry_definition_replaces_a_second_authored_one():
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    recs, notes = adopt_registry_definitions(
        [_record(error_def=_OLD)], _registry(Error=_NEW))
    assert dict(recs[0].defs)["Error"] == _NEW
    assert any("already fixed" in n for n in notes)


def test_registry_agreement_is_a_silent_no_op():
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    recs, notes = adopt_registry_definitions(
        [_record(error_def=_NEW)], _registry(Error=_NEW))
    assert dict(recs[0].defs)["Error"] == _NEW
    assert notes == []


def test_rewrapping_is_not_a_redefinition():
    # The record and the registry are rendered by the same extractor but not
    # necessarily wrapped identically. A rewrap must not read as a second
    # definition, or every start would log — and replan — the whole component.
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    rewrapped = _NEW.replace(" { ", " {\n    ").replace(" }", "\n}")
    recs, notes = adopt_registry_definitions(
        [_record(error_def=_NEW)], _registry(Error=rewrapped))
    assert dict(recs[0].defs)["Error"] == _NEW      # the record's text is kept
    assert notes == []


def test_uid_the_registry_does_not_hold_is_left_alone():
    # A genuinely cluster-private item has no crate-wide definition to adopt.
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    recs, notes = adopt_registry_definitions([_record(error_def=_OLD)], {})
    assert dict(recs[0].defs)["Error"] == _OLD
    assert notes == []


def test_adopted_definition_is_then_planned_into_the_live_tree():
    # End to end: the substitution is worthless unless it reaches the tree.
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    recs, _ = adopt_registry_definitions(
        [_record(error_def=_OLD)], _registry(Error=_NEW))
    plan, _notes = plan_layout_reconciliation(
        _surface(), recs, lambda rel: _OLD + "\n" + _IMPL_OLD)
    assert [r.new for r in plan["lib.rs"]] == [_NEW]


def test_non_authored_records_are_untouched():
    from pipeline_core.application.reconcile_layout import (
        adopt_registry_definitions,
    )
    gated = LayoutComponentRecord(
        frozenset({"Error"}), LayoutOutcome.GATED, (("Error", _OLD),))
    recs, notes = adopt_registry_definitions([gated], _registry(Error=_NEW))
    assert recs[0] is gated
    assert notes == []


_TAG_AUTHORED = ("/// Picks the tagged arm.\n#[doc(hidden)]\n"
                 "pub trait TagKind: Sized {\n"
                 "    fn kind(&self) -> Tag { unimplemented!() }\n}")
_TAG_FILLED_DECL = ("#[doc(hidden)]\npub trait TagKind: Sized {\n"
                    "    fn kind(&self) -> Tag {\n        Tag\n    }\n}")


def _tag_plan(live_text: str):
    surface = classify_private_surface([
        TypeRecord(file="kind.rs", kind="trait", name="TagKind", is_pub=False,
                   definition=_TAG_AUTHORED),
    ], [], cfg_cluster="stable", author_private_fields=True)
    comp = next(c for c in surface.components
                if any(i.name == "TagKind" for i in c))
    rec = LayoutComponentRecord(
        frozenset(i.name for i in comp), LayoutOutcome.AUTHORED,
        tuple((uid, _TAG_AUTHORED) for uid in component_uids(comp)))
    return plan_layout_reconciliation(surface, [rec], lambda rel: live_text)


def test_a_filled_member_body_is_not_a_superseded_definition() -> None:
    """Run `3893442-cb547388` (2026-09-19): `kind.rs`'s two tag traits were
    "superseded" on every launch because the check compared the tree with
    the AUTHORED text (stub members) while the tree held filled members, and
    each reinstall replaced the declaration below the doc comment with a
    text carrying the doc comment — seven copies by then. The sentence: a
    tree holding the doc comment once and the filled member plans nothing;
    a tree holding it three times is healed to once in one reinstall.
    Control: the filled tree with no doc comment at all still gets it
    (one reinstall, then nothing)."""
    doc = "/// Picks the tagged arm.\n"
    plan, _notes = _tag_plan("pub struct Tag;\n" + doc + _TAG_FILLED_DECL + "\n")
    assert plan == {}

    plan, _notes = _tag_plan("pub struct Tag;\n" + doc * 3 + _TAG_FILLED_DECL + "\n")
    (rep,) = plan["kind.rs"]
    assert rep.old.count("/// Picks") == 3
    assert rep.new.count("/// Picks") == 1 and "Tag\n" in rep.new
    healed = ("pub struct Tag;\n" + doc * 3 + _TAG_FILLED_DECL + "\n").replace(
        rep.old, rep.new)
    assert _tag_plan(healed)[0] == {}

    plan, _notes = _tag_plan("pub struct Tag;\n" + _TAG_FILLED_DECL + "\n")
    (rep,) = plan["kind.rs"]
    once = ("pub struct Tag;\n" + _TAG_FILLED_DECL + "\n").replace(rep.old, rep.new)
    assert once.count("/// Picks") == 1 and _tag_plan(once)[0] == {}


# --- a stored definition that dropped a frozen field visibility ---------------
# The registry keeps the first text `setdefault` stored. Reconciliation used
# to match that text, so a live file that still held it planned nothing and
# the private field stayed private. The install matches the carried text.

_FROZEN_WRAP = "#[repr(transparent)]\npub struct Wrap<M>(pub M);"
_STORED_WRAP = ("/// Holds the payload.\n"
                "#[repr(transparent)]\npub struct Wrap<M>(M);")


def _wrap_surface():
    return classify_private_surface([
        TypeRecord(file="wrap.rs", kind="struct", name="Wrap", is_pub=False,
                   definition=_FROZEN_WRAP),
    ], [], cfg_cluster="stable")


def _wrap_record():
    surface = _wrap_surface()
    comp = next(iter(surface.components))
    return surface, LayoutComponentRecord(
        frozenset(i.name for i in comp), LayoutOutcome.AUTHORED,
        tuple((uid, _STORED_WRAP) for uid in component_uids(comp)))


def test_a_stored_private_field_is_reinstalled_at_its_frozen_visibility():
    """The live file holds the stored definition, so a match on the stored
    text plans nothing. The carried text is what gets installed, and the
    same stored row against that installed text plans nothing — the private
    field is not put back."""
    surface, rec = _wrap_record()
    plan, notes = plan_layout_reconciliation(
        surface, [rec], lambda rel: _STORED_WRAP + "\n")
    assert "wrap.rs" in plan, notes
    (rep,) = plan["wrap.rs"]
    assert "(pub M);" in rep.new and "(M);" not in rep.new
    assert any("superseded" in n for n in notes)
    again, _ = plan_layout_reconciliation(
        surface, [rec], lambda rel: rep.new + "\n")
    assert again == {}


def test_without_the_carry_the_stored_private_field_stays_installed(monkeypatch):
    """Negative control: identity carry is the pre-fix match. The live file
    holds the stored text, so the plan is empty and the private field stays."""
    import pipeline_core.application.reconcile_layout as mod
    monkeypatch.setattr(
        mod, "carry_stored_definition", lambda item, stored: stored)
    surface, rec = _wrap_record()
    plan, _notes = plan_layout_reconciliation(
        surface, [rec], lambda rel: _STORED_WRAP + "\n")
    assert plan == {}
