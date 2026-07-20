"""Pins for F11 private-surface classification (freeze-scope boundary)."""
from __future__ import annotations

from pipeline_core.domain.models import (
    FnRecord,
    ModRecord,
    TypeRecord,
    UseRecord,
)
from pipeline_core.domain.private_surface import (
    SurfaceItemKind,
    classify_private_surface,
    connected_components,
)
from pipeline_core.domain.rust_name_scope import ModuleScopes


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


def test_pub_crate_helper_fn_referencing_layout_is_replaceable():
    # `pub(crate) fn helper` (is_pub folds it to True, is_exactly_pub False)
    # is NOT external API — it must be offered for authoring, not excluded as
    # if it were a frozen public fn.
    types = _anyhow_surface()
    fns = [FnRecord(
        file="src/error.rs", fn_name="vtable",
        sig_text="unsafe fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable",
        is_pub=True, is_exactly_pub=False)]
    s = classify_private_surface(types, fns)
    fn_items = [i.name for i in s.replaceable if i.kind is SurfaceItemKind.FN_SIG]
    assert fn_items == ["vtable"]


def test_truly_pub_fn_is_not_offered_for_authoring():
    types = _anyhow_surface()
    fns = [FnRecord(
        file="src/error.rs", fn_name="vtable",
        sig_text="pub unsafe fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable",
        is_pub=True, is_exactly_pub=True)]
    s = classify_private_surface(types, fns)
    assert not [i for i in s.replaceable if i.kind is SurfaceItemKind.FN_SIG]


def test_scoped_names_do_not_weld_a_rebound_identifier():
    # The real anyhow collision: the crate root does `use std::error::Error as
    # StdError` while an inline `mod ext` in context.rs declares a private
    # trait of the same name. Matched bare, every `dyn StdError` signature in
    # the crate became an edge to that private trait — welding trait impls on
    # PUBLIC types into the private-layout component and offering them for
    # re-authoring.
    types = [
        _ty("Error", "struct", True, "pub struct Error { inner: Own<ErrorImpl> }", "lib.rs"),
        _ty("StdError", "trait", False, "pub trait StdError { }", "context.rs"),
        _ty("Own", "struct", False, "struct Own<T> { ptr: NonNull<T> }", "ptr.rs"),
        _ty("ErrorImpl", "struct", False, "struct ErrorImpl { o: () }", "error.rs"),
    ]
    fns = [FnRecord(file="error.rs", fn_name="source",
                    sig_text="fn source(&self) -> Option<&(dyn StdError + 'static)>",
                    is_pub=False, is_exactly_pub=False)]
    mods = [ModRecord(name=n, file=f, declared_in="lib.rs", vis="private")
            for n, f in (("context", "context.rs"), ("ptr", "ptr.rs"),
                         ("error", "error.rs"))]
    uses = [UseRecord(file="lib.rs", alias="StdError",
                      path=("std", "error", "Error")),
            UseRecord(file="error.rs", alias="StdError", path=("crate", "StdError")),
            UseRecord(file="error.rs", alias="Own", path=("crate", "ptr", "Own")),
            UseRecord(file="lib.rs", alias="Own", path=("crate", "ptr", "Own")),
            UseRecord(file="lib.rs", alias="ErrorImpl",
                      path=("crate", "error", "ErrorImpl"))]
    scopes = ModuleScopes.from_records(mods, uses)

    bare = classify_private_surface(types, fns)
    assert "source" in {i.name for i in bare.replaceable}
    assert frozenset({"StdError", "source"}) in {
        frozenset(i.name for i in c) for c in bare.components}

    scoped = classify_private_surface(types, fns, scopes=scopes)
    assert "source" not in {i.name for i in scoped.replaceable}
    assert {frozenset(i.name for i in c) for c in scoped.components} == {
        frozenset({"StdError"}), frozenset({"Own", "ErrorImpl"})}


def test_frozen_item_couples_the_private_types_it_names_together():
    # `impl<T> Own<T> { pub fn by_mut(&mut self) -> Mut<T> }` stays frozen, so
    # re-authoring `Own` without `Mut` breaks it — neither type's own
    # definition names the other, yet they must be authored as one component.
    types = [
        _ty("Own", "struct", False, "struct Own<T> { ptr: NonNull<T> }", "ptr.rs"),
        _ty("Mut", "struct", False, "struct Mut<T> { ptr: NonNull<T> }", "ptr.rs"),
    ]
    fns = [FnRecord(file="ptr.rs", fn_name="by_mut", container="impl Own < T >",
                    sig_text="fn by_mut(&mut self) -> Mut<T>", is_pub=True)]
    s = classify_private_surface(types, fns)
    assert s.coupled == (frozenset({"Mut", "Own"}),)
    assert len(s.components) == 1


def test_trait_default_method_is_not_a_separate_target():
    """anyhow's kind.rs: `trait AdhocKind { fn anyhow_kind(&self) -> Adhoc {…} }`.
    The trait's definition text CONTAINS the method, so listing the method too
    gives the splice two nested spans — refused as ambiguous, freezing the whole
    9-item component on every attempt, on both clusters."""
    types = [TypeRecord(file="kind.rs", kind="trait", name="AdhocKind", is_pub=False,
                        definition=("pub trait AdhocKind: Sized {\n"
                                    "    fn anyhow_kind(&self) -> Adhoc {}\n}")),
             TypeRecord(file="kind.rs", kind="struct", name="Adhoc", is_pub=False,
                        definition="pub struct Adhoc;")]
    fns = [FnRecord(file="kind.rs", fn_name="anyhow_kind", is_pub=False,
                    is_exactly_pub=False, container="trait AdhocKind",
                    sig_text="fn anyhow_kind(&self) -> Adhoc")]
    s = classify_private_surface(types, fns, cfg_cluster="stable")
    assert "anyhow_kind" not in {i.name for i in s.replaceable}
    assert {"AdhocKind", "Adhoc"} == {i.name for i in s.replaceable}


def test_inherent_impl_method_stays_authorable():
    """`impl Adhoc { fn new(..) }` lives OUTSIDE `pub struct Adhoc;` — no span
    nesting, so it must remain its own target."""
    types = [TypeRecord(file="kind.rs", kind="struct", name="Adhoc", is_pub=False,
                        definition="pub struct Adhoc;")]
    fns = [FnRecord(file="kind.rs", fn_name="new", is_pub=False,
                    is_exactly_pub=False, container="impl Adhoc",
                    sig_text="fn new(self) -> Adhoc")]
    s = classify_private_surface(types, fns, cfg_cluster="stable")
    assert "new" in {i.name for i in s.replaceable}
