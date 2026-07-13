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
    types = [
        _ty("A", "struct", False, "struct A { b: B }"),
        _ty("B", "struct", False, "struct B { x: u8 }"),
        _ty("C", "struct", False, "struct C { d: D }"),
        _ty("D", "struct", False, "struct D { y: u8 }"),
    ]
    comps = classify_private_surface(types, []).components
    assert len(comps) == 2
    assert {frozenset(i.name for i in c) for c in comps} == {
        frozenset({"A", "B"}), frozenset({"C", "D"})}


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
