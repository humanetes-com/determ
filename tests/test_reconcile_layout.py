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
