"""Pins for F11 private-surface classification (freeze-scope boundary)."""
from __future__ import annotations

from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.private_surface import (
    SurfaceItemKind,
    classify_private_surface,
    connected_components,
)


def _ty(name, kind, is_pub, defn, file="src/error.rs"):
    return TypeRecord(file=file, kind=kind, name=name, definition=defn, is_pub=is_pub)


def _anyhow_surface():
    # The real freeze-cascade shape: pub Error → priv Own → priv ErrorImpl → priv ErrorVTable.
    return [
        _ty("Error", "struct", True, "pub struct Error { inner: Own<ErrorImpl> }", "src/lib.rs"),
        _ty("Own", "struct", False, "struct Own<T> { ptr: NonNull<T> }", "src/ptr.rs"),
        _ty("ErrorImpl", "struct", False,
            "struct ErrorImpl { vtable: &'static ErrorVTable, object: () }"),
        _ty("ErrorVTable", "struct", False,
            "struct ErrorVTable { object_drop: unsafe fn(Own<ErrorImpl>) }"),
    ]


def test_public_frozen_private_replaceable():
    s = classify_private_surface(_anyhow_surface(), [])
    assert s.frozen_public == frozenset({"Error"})
    names = {i.name for i in s.replaceable}
    assert names == {"Own", "ErrorImpl", "ErrorVTable"}
    assert not s.is_empty


def test_references_form_edges():
    s = classify_private_surface(_anyhow_surface(), [])
    by = {i.name: i for i in s.replaceable}
    assert "ErrorVTable" in by["ErrorImpl"].references
    assert "ErrorImpl" in by["ErrorVTable"].references  # via Own<ErrorImpl>


def test_connected_component_is_single_for_anyhow_core():
    # The private layout is one connected component → authored in ONE turn.
    s = classify_private_surface(_anyhow_surface(), [])
    comps = s.components
    assert len(comps) == 1
    assert {i.name for i in comps[0]} == {"Own", "ErrorImpl", "ErrorVTable"}


def test_disjoint_private_types_split_into_two_components():
    # NB: camelcase_type_names requires 2+ chars, so single-letter names
    # (`A`/`B`) never match — this fixture uses real multi-char names.
    types = [
        _ty("Aa", "struct", False, "struct Aa { b: Bb }"),
        _ty("Bb", "struct", False, "struct Bb { x: u8 }"),
        _ty("Cc", "struct", False, "struct Cc { d: Dd }"),
        _ty("Dd", "struct", False, "struct Dd { y: u8 }"),
    ]
    comps = classify_private_surface(types, []).components
    assert len(comps) == 2
    assert {frozenset(i.name for i in c) for c in comps} == {
        frozenset({"Aa", "Bb"}), frozenset({"Cc", "Dd"})}


def test_private_helper_fn_referencing_layout_is_replaceable():
    types = _anyhow_surface()
    fns = [
        FnRecord(file="src/error.rs", fn_name="vtable",
                 sig_text="unsafe fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable",
                 is_pub=False),
        FnRecord(file="src/lib.rs", fn_name="unrelated",
                 sig_text="fn unrelated(x: u8) -> u8", is_pub=False),
    ]
    s = classify_private_surface(types, fns)
    fn_items = [i for i in s.replaceable if i.kind is SurfaceItemKind.FN_SIG]
    assert [i.name for i in fn_items] == ["vtable"]
    # the helper attaches to the ErrorImpl/ErrorVTable component.
    comps = s.components
    for c in comps:
        if any(i.name == "vtable" for i in c):
            assert {"ErrorImpl", "ErrorVTable"} <= {i.name for i in c}


def test_empty_when_all_public():
    types = [_ty("Error", "struct", True, "pub struct Error;", "src/lib.rs")]
    s = classify_private_surface(types, [])
    assert s.is_empty and s.components == ()


def test_components_helper_directly():
    assert connected_components(()) == ()


def test_pub_crate_type_is_replaceable_not_frozen():
    # anyhow's real shape: `pub(crate) struct ErrorImpl` — `is_pub` folds this
    # to True, but it is NOT part of the crate's external API.
    types = [
        _ty("Error", "struct", True, "pub struct Error { inner: Own<ErrorImpl> }", "src/lib.rs"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl",
                   definition="struct ErrorImpl { vtable: &'static ErrorVTable }",
                   is_pub=True, is_exactly_pub=False),
        _ty("ErrorVTable", "struct", False, "struct ErrorVTable {}"),
    ]
    s = classify_private_surface(types, [])
    assert s.frozen_public == frozenset({"Error"})
    assert {i.name for i in s.replaceable} == {"ErrorImpl", "ErrorVTable"}


def test_pub_item_behind_unreachable_module_is_replaceable():
    # `pub struct Own<T>` declared in a PRIVATE `mod ptr;` — item-level pub
    # alone must not freeze it once module-reachability data is available.
    types = [
        _ty("Error", "struct", True, "pub struct Error { inner: Own<ErrorImpl> }", "src/lib.rs"),
        _ty("Own", "struct", True, "pub struct Own<T> { ptr: NonNull<T> }", "src/ptr.rs"),
    ]
    reachable = frozenset({"src/lib.rs"})  # src/ptr.rs NOT reachable
    s = classify_private_surface(types, [], reachable_files=reachable)
    assert s.frozen_public == frozenset({"Error"})
    assert {i.name for i in s.replaceable} == {"Own"}


def test_reachable_files_none_skips_module_check():
    # omitting reachable_files preserves the OLD behaviour (item-vis only).
    types = [_ty("Own", "struct", True, "pub struct Own<T>;", "src/ptr.rs")]
    s = classify_private_surface(types, [])
    assert s.frozen_public == frozenset({"Own"})


def test_reachable_from_public_scopes_to_one_public_type():
    # `Error`'s OWN definition text names both `Own` and `ErrorImpl` directly
    # (`Own<ErrorImpl>`); the closure also picks up `ErrorVTable` via ErrorImpl.
    s = classify_private_surface(_anyhow_surface(), [])
    assert s.public_direct_refs["Error"] == frozenset({"Own", "ErrorImpl"})
    assert s.reachable_from_public("Error") == {"Own", "ErrorImpl", "ErrorVTable"}


def test_reachable_from_public_empty_for_unknown_or_unrelated_type():
    s = classify_private_surface(_anyhow_surface(), [])
    assert s.reachable_from_public("NoSuchType") == frozenset()


def test_public_direct_refs_do_not_merge_across_unrelated_public_types():
    types = [
        _ty("Error", "struct", True, "pub struct Error { inner: Own<ErrorImpl> }", "src/lib.rs"),
        _ty("Own", "struct", False, "struct Own<T> { ptr: NonNull<T> }", "src/ptr.rs"),
        _ty("ErrorImpl", "struct", False, "struct ErrorImpl { x: u8 }"),
        _ty("Other", "struct", True, "pub struct Other { inner: OtherImpl }", "src/lib.rs"),
        _ty("OtherImpl", "struct", False, "struct OtherImpl { y: u8 }", "src/other.rs"),
    ]
    s = classify_private_surface(types, [])
    assert s.reachable_from_public("Error") == {"Own", "ErrorImpl"}
    assert s.reachable_from_public("Other") == {"OtherImpl"}
