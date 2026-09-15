"""File-level `use` items — the scope a transplanted fn body resolves in.

The live failure these pin: the cross-cluster seed placed 121 bodies in exactly
the right slots and the cluster still opened RED on 41 `E0425 cannot find … in
this scope`, because `use crate::wrapper::MessageError;` was earned by the DONOR
tree's own fill and the frozen skeletons are byte-identical without it.
"""
from __future__ import annotations

from pipeline_core.domain.rust_use_items import (
    insert_use_items,
    missing_use_items,
    use_items,
)


def test_reads_top_level_uses_with_their_cfg_attributes() -> None:
    src = (
        "#![allow(unused)]\n"
        "use crate::a::Alpha;\n"
        "#[cfg(feature = \"std\")]\n"
        "use crate::b::Beta;\n"
    )
    items = use_items(src)
    assert [i.imported for i in items] == ["Alpha", "Beta"]
    # The cfg is part of the item: copied without it, a gated import becomes a
    # hard error in the cluster that does not have the feature.
    assert items[1].text.startswith("#[cfg(feature = \"std\")]")


def test_ignores_uses_that_are_not_file_level() -> None:
    src = (
        "use crate::real::Thing;\n"
        "mod inner {\n"
        "    use crate::inner_only::Nested;\n"
        "}\n"
        "/// use crate::doc::Example;\n"
    )
    assert [i.imported for i in use_items(src)] == ["Thing"]


def test_alias_and_glob_binding_names() -> None:
    src = "use crate::a::Long as Short;\nuse crate::b::*;\nuse crate::c::{X, Y};\n"
    items = use_items(src)
    assert items[0].imported == "Short"
    assert items[1].is_glob
    # A brace group binds several names; it is matched by exact text instead.
    assert items[2].is_glob


def test_missing_items_are_the_donors_the_target_lacks() -> None:
    donor = ("use crate::wrapper::DisplayError;\n"
             "use crate::wrapper::MessageError;\n"
             "use crate::ptr::Own;\n")
    target = "use crate::ptr::Own;\n"
    assert [i.imported for i in missing_use_items(target, donor)] == [
        "DisplayError", "MessageError"]


def test_a_name_already_bound_is_never_re_added() -> None:
    # Same name by a different path is a real conflict (E0659) and a duplicate
    # is E0252 — either way "already bound here" means leave it alone.
    donor = "use crate::wrapper::MessageError;\n"
    target = "use crate::other::MessageError;\n"
    assert missing_use_items(target, donor) == []


def test_trait_import_needed_only_for_method_resolution_is_carried() -> None:
    # The case a reference-scan filter would miss: nothing NAMES `Context` as a
    # path head, but `.context(…)` does not resolve without the trait in scope.
    donor = "use crate::ext::Context;\n"
    target = "use crate::ptr::Own;\n"
    body = "fn f(e: E) { e.context(\"x\"); }\n"
    assert [i.imported for i in missing_use_items(target + body, donor)] == ["Context"]


def test_inserted_after_the_last_existing_use() -> None:
    target = "#![allow(unused)]\nuse crate::a::Alpha;\n\nfn f() {}\n"
    out = insert_use_items(target, missing_use_items(target, "use crate::b::Beta;\n"))
    assert out == ("#![allow(unused)]\nuse crate::a::Alpha;\n"
                   "use crate::b::Beta;\n\nfn f() {}\n")


def test_inserted_after_inner_attributes_when_the_file_has_no_use() -> None:
    # An import above `#![…]` makes the inner attribute no longer first — a hard
    # error, and the one placement that must never happen.
    target = "#![no_std]\n#![allow(unused)]\n\nfn f() {}\n"
    out = insert_use_items(target, missing_use_items(target, "use crate::b::Beta;\n"))
    assert out.startswith("#![no_std]\n#![allow(unused)]\nuse crate::b::Beta;\n")


def test_insert_is_a_no_op_without_items() -> None:
    target = "use crate::a::Alpha;\n"
    assert insert_use_items(target, []) == target


# --- layout divergence: a body is written against a SHAPE ---

_STABLE_ERROR = """\
#[repr(C)]
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Backtrace,
    _error: E,
}
pub(crate) struct Mut<'a, T> { pub(crate) ptr: NonNull<T>, k: &'a () }
pub(crate) struct Same { a: u32 }
"""
_NIGHTLY_ERROR = """\
#[repr(C)]
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    _data: E,
}
pub(crate) struct Mut<'a, T> { ptr: NonNull<T>, k: &'a () }
pub(crate) struct Same { a: u32 }
"""


def test_divergent_items_are_the_types_the_two_clusters_shaped_differently() -> None:
    from pipeline_core.domain.rust_use_items import divergent_item_names
    div = divergent_item_names(_NIGHTLY_ERROR, _STABLE_ERROR)
    assert "ErrorImpl" in div       # its field SET differs: a literal cannot move
    # `Mut` changed one field's visibility and nothing else. The field is what a
    # body depending on the change must name; `Mut::…` calls that never touch
    # `ptr` compile against either declaration.
    assert "Mut" not in div
    assert "ptr" in div
    assert "Same" not in div        # identical in both — nothing to refuse


def test_divergence_carries_the_field_names_a_body_reaches_through_self() -> None:
    """A body says `self._error`, never `ErrorImpl::_error`, so matching the
    type name alone leaves `no field '_error'` behind."""
    from pipeline_core.domain.rust_use_items import divergent_item_names
    div = divergent_item_names(_NIGHTLY_ERROR, _STABLE_ERROR)
    assert {"_error", "backtrace", "_data"} <= div


def test_a_field_that_only_changed_VISIBILITY_still_diverges() -> None:
    """`Mut.ptr` is declared in both and `pub(crate)` in only one: same name,
    different meaning, `E0616 field 'ptr' is private` in the target."""
    from pipeline_core.domain.rust_use_items import divergent_item_names
    assert "ptr" in divergent_item_names(_NIGHTLY_ERROR, _STABLE_ERROR)


def test_identical_files_diverge_on_nothing() -> None:
    from pipeline_core.domain.rust_use_items import divergent_item_names
    assert divergent_item_names(_STABLE_ERROR, _STABLE_ERROR) == frozenset()


def test_seed_refuses_a_body_that_names_a_divergent_field() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    donor = _STABLE_ERROR + (
        "impl<E> ErrorImpl<E> {\n"
        "    fn bt(&self) -> u32 { self.backtrace }\n"
        "    fn plain(&self) -> u32 { 7 }\n"
        "}\n")
    target = _NIGHTLY_ERROR + (
        "impl<E> ErrorImpl<E> {\n"
        "    fn bt(&self) -> u32 { ::core::unimplemented!() }\n"
        "    fn plain(&self) -> u32 { ::core::unimplemented!() }\n"
        "}\n")
    r = backfill_stub_bodies(target, [donor])
    # The body reaching a divergent field is left as a stub for this cluster's
    # own fill loop; the one that does not is adopted normally.
    assert "self.backtrace" not in r.text
    assert "{ 7 }" in r.text
    # Counted as a LAYOUT refusal, not an ambiguous one: the merge knew exactly
    # which slot the body belonged in — the body just cannot compile against
    # this cluster's declarations. Same remedy, different finding.
    assert r.filled == 1
    assert r.layout_skipped == 1
    assert r.ambiguous_skipped == 0
