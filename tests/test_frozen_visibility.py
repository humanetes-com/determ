"""The frozen module-external visibility is carried onto an authored layout,
and the carried text is the one every store of the committed layout holds.

The defect, run `673664-014fd53a` (2026-10-14, cluster `stable`): the frozen
definition of a wrapper type in a private module was a tuple struct with a
`pub` field; t1.5's replacement dropped the field's `pub`. Its constructor is
called from another file, so every route that wrote that caller failed on
`error[E0603]: tuple struct constructor … is private`, and no route can
change a field's visibility. A carry applied only inside the splice would
also have been undone: the registry, the locate keys and the authored items
were built from the model's text, and the cluster-start reconciliation
reinstalls a stored definition the live tree does not contain verbatim.

The shapes here name no crate symbol.
"""
from __future__ import annotations

import pytest

from pipeline_core.application import author_layout as al
from pipeline_core.application.author_layout import AuthorLayoutDeps, author_layout
from pipeline_core.domain.authored_layout import AuthoredItem, carry_frozen_facts
from pipeline_core.domain.frozen_visibility import (
    carry_frozen_visibility,
    narrower,
)
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
    classify_private_surface,
)
from pipeline_core.domain.scaffold_rewrite import find_definition_span

_FROZEN_WRAP = "#[repr(transparent)]\npub struct Wrap<M>(pub M);"
_AUTHORED_WRAP = ("/// Holds the payload so its own TypeId is what a downcast sees.\n"
                  "#[repr(transparent)]\npub struct Wrap<M>(M);")


def test_a_tuple_field_made_private_gets_its_frozen_visibility_back() -> None:
    out = carry_frozen_visibility(_FROZEN_WRAP, _AUTHORED_WRAP)
    assert out == ("/// Holds the payload so its own TypeId is what a downcast sees.\n"
                   "#[repr(transparent)]\npub struct Wrap<M>(pub M);")


def test_constructibility_raises_every_redesigned_field_to_the_narrowest() -> None:
    """Every frozen field visible outside the module (`pub`, `pub(crate)`):
    every authored field, new names included, is at least `pub(crate)`; a
    kept field keeps its own wider `pub`. Attributes and docs on a field and
    a fn-pointer type's `->` do not disturb the field split."""
    frozen = "pub struct S { pub a: u8, pub(crate) b: Vec<fn(u8) -> u8> }"
    authored = ("struct S {\n    /// doc\n    #[allow(dead_code)]\n    a: u8,\n"
                "    c: Map<K, V>,\n}")
    assert carry_frozen_visibility(frozen, authored) == (
        "pub struct S {\n    /// doc\n    #[allow(dead_code)]\n    pub a: u8,\n"
        "    pub(crate) c: Map<K, V>,\n}")


def test_a_struct_with_a_private_field_carries_only_kept_members() -> None:
    """One frozen field private: no struct literal reached it from outside,
    so new fields stay as authored; the kept `pub` field and the head are
    raised."""
    frozen = "pub(crate) struct S { a: u8, pub b: u8 }"
    authored = "struct S { b: u8, a: u8, z: u8 }"
    assert carry_frozen_visibility(frozen, authored) == (
        "pub(crate) struct S { pub b: u8, a: u8, z: u8 }")


def test_never_narrows_and_leaves_incomparable_paths_alone() -> None:
    assert carry_frozen_visibility("pub struct S(pub u8);",
                                   "pub struct S(pub u8);") == "pub struct S(pub u8);"
    assert carry_frozen_visibility("pub(crate) struct S(u8);",
                                   "pub struct S(pub u8);") == "pub struct S(pub u8);"
    # pub(in a) and pub(super) do not compare: rule 1 carries nothing, and the
    # kept field of the same index is raised only along its own order.
    assert carry_frozen_visibility(
        "pub struct S(pub(in crate::a) u8, pub(super) u8);",
        "pub struct S(u8, u8, u8);") == "pub struct S(u8, u8, u8);"
    assert narrower("pub(in crate::a)", "pub(super)") is None
    assert narrower("", "pub(in crate::a)") is True
    assert narrower("pub(crate)", "pub") is True


def test_a_fn_head_made_private_is_raised() -> None:
    assert carry_frozen_visibility(
        "pub(crate) unsafe fn f(x: u8) -> u8 { unimplemented!() }",
        "unsafe fn f(x: u8) -> u8 { unimplemented!() }") == (
        "pub(crate) unsafe fn f(x: u8) -> u8 { unimplemented!() }")


def test_the_carry_is_idempotent() -> None:
    once = carry_frozen_visibility(_FROZEN_WRAP, _AUTHORED_WRAP)
    assert carry_frozen_visibility(_FROZEN_WRAP, once) == once


def test_public_field_types_get_no_visibility() -> None:
    """A public type whose private fields are model-authored: its frozen
    fields are elided and `_public_contract_broken` refuses a `pub` field,
    so the facts carry the repr only."""
    frozen = "#[repr(transparent)]\npub struct E {\n    inner: X,\n}"
    items = (PrivateSurfaceItem(kind=SurfaceItemKind.PUB_FIELDS, name="E",
                                file="src/lib.rs", definition=frozen),)
    out = carry_frozen_facts(items, {"E": AuthoredItem(
        "E", "struct", "pub struct E {\n    inner: Y,\n}")})
    assert out["E"].definition == "#[repr(transparent)]\npub struct E {\n    inner: Y,\n}"


# --- the stored layout is the carried one -------------------------------------

def _surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<u8> }"),
        TypeRecord(file="src/wrap.rs", kind="struct", name="Wrap", is_pub=False,
                   definition=_FROZEN_WRAP),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _run(files: dict[str, str]):
    reply = (f'<LAYOUT_ITEM name="Wrap" kind="struct">{_AUTHORED_WRAP}'
             '</LAYOUT_ITEM>')
    deps = AuthorLayoutDeps(
        run_model=lambda p: reply, read_skeleton=files.get,
        write_skeleton=files.__setitem__,
        compile_check=lambda: (True, ""), log=lambda m: None, max_attempts=1)
    return author_layout(_surface(), module_name="m", public_surface="",
                         select_pins=lambda comp: [], symbol_inventory="",
                         deps=deps)


def test_the_committed_layout_stores_the_carried_text() -> None:
    """Plan sentence: the registry item, the locate key and the skeleton hold
    one text, the carried one; reconciliation then finds it installed.
    Negative control: with the carry removed from the commit, the stored
    text is the model's `(M)`, which the live tree does not contain, so the
    reconciliation would reinstall it."""
    files = {"src/wrap.rs": _FROZEN_WRAP + "\n"}
    res = _run(files)
    (comp,) = [c for c in res.components if c.authored]
    stored = dict(comp.authored_items)["Wrap"].definition
    key = dict(comp.authored_defs)["Wrap"]
    assert "(pub M);" in stored and key == stored
    assert "(pub M);" in files["src/wrap.rs"]
    assert find_definition_span(files["src/wrap.rs"], stored) is not None

    files = {"src/wrap.rs": _FROZEN_WRAP + "\n"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(al, "carry_frozen_facts", lambda items, authored: dict(authored))
        before = _run(files)
    (comp,) = [c for c in before.components if c.authored]
    stored = dict(comp.authored_items)["Wrap"].definition
    assert "(M);" in stored and "(pub M)" not in stored


# --- an already-stored layout is carried at cluster start --------------------
# `setdefault` keeps the first definition. A registry and a skeleton written
# before the carry still hold the model's `(M)`. Cluster start is what a
# task2a restart runs; it has to install the carried field and store that
# text, or the next seed copies the private field back.

_STORED = ("/// Holds the payload.\n"
           "#[repr(transparent)]\npub struct Wrap<M>(M);")


def _stored_tree(tmp_path):
    from pipeline_core.adapters.layout_outcome_store import save_layout_records
    from pipeline_core.adapters.layout_registry_store import save_registry
    from pipeline_core.application.author_layout import (
        LayoutComponentRecord,
        LayoutOutcome,
    )
    from pipeline_core.domain.authored_layout import component_uids
    from pipeline_core.domain.models import TypeRecord
    from pipeline_core.domain.private_surface import classify_private_surface

    # Item file is relative to src/: the sync reads filled/src and skeleton/src.
    surface = classify_private_surface([
        TypeRecord(file="wrap.rs", kind="struct", name="Wrap", is_pub=False,
                   definition=_FROZEN_WRAP),
    ], [], cfg_cluster="stable")
    comp = next(iter(surface.components))
    rec = LayoutComponentRecord(
        frozenset(i.name for i in comp), LayoutOutcome.AUTHORED,
        tuple((uid, _STORED) for uid in component_uids(comp)))
    save_layout_records(tmp_path, "stable", [rec])
    save_registry(tmp_path, {"Wrap": AuthoredItem("Wrap", "struct", _STORED)})
    for root in (tmp_path / "filled" / "src",
                 tmp_path / ".skeleton" / "stable" / "src"):
        root.mkdir(parents=True, exist_ok=True)
        (root / "wrap.rs").write_text(_STORED + "\n", encoding="utf-8")
    return surface


class _Tel:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, m: str) -> None:
        self.lines.append(m)

    def emit(self, *_a, **_k) -> None:
        pass


class _Cargo:
    def __init__(self, rcs: list[int]) -> None:
        self.rcs = rcs
        self.calls = 0

    def cluster_env(self, _cluster) -> dict:
        return {}

    def run_check_diag(self, *_a, **_k):
        rc = self.rcs[min(self.calls, len(self.rcs) - 1)]
        self.calls += 1
        return rc, ("error[E0308]: mismatched types" if rc else ""), []


class _Cluster:
    name = "stable"
    toolchain = None


def _drive(tmp_path, rcs: list[int]):
    from pipeline_core.adapters.layout_outcome_store import (
        load_layout_records,
        save_layout_records,
    )
    from pipeline_core.adapters.layout_registry_store import (
        load_registry,
        save_registry,
    )
    from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree
    from pipeline_core.application.generators.task2_generator import (
        StaticSkeletonTask2Generator as Gen,
    )

    surface = _stored_tree(tmp_path)
    tel = _Tel()
    gen = Gen.__new__(Gen)
    object.__setattr__(gen, "tel", tel)
    object.__setattr__(gen, "cargo", _Cargo(rcs))
    object.__setattr__(gen, "layout_sync", (
        lambda _pd, _c: surface,
        load_layout_records,
        apply_layout_to_tree,
    ))
    object.__setattr__(gen, "layout_registry", load_registry)
    object.__setattr__(gen, "save_layout_registry", save_registry)
    object.__setattr__(gen, "save_layout_records", save_layout_records)
    gen._sync_layout_into_tree(
        "[tag]", tmp_path, _Cluster(), tmp_path / "filled",
        skeleton_dir=tmp_path / ".skeleton" / "stable")
    return tel


def test_cluster_start_carries_an_already_stored_private_field(tmp_path):
    """The filled tree, the skeleton, the registry and the locate key all
    hold the carried field after one cluster start. A second start plans
    nothing and does not put the private field back."""
    from pipeline_core.adapters.layout_outcome_store import load_layout_records
    from pipeline_core.adapters.layout_registry_store import load_registry

    tel = _drive(tmp_path, [0, 0])
    assert not any("skipped:" in m for m in tel.lines), tel.lines
    assert any("carried frozen facts" in m for m in tel.lines), tel.lines
    filled = (tmp_path / "filled" / "src" / "wrap.rs").read_text(encoding="utf-8")
    skel = (tmp_path / ".skeleton" / "stable" / "src" / "wrap.rs").read_text(
        encoding="utf-8")
    reg = load_registry(tmp_path)["Wrap"].definition
    (rec,) = load_layout_records(tmp_path, "stable")
    key = dict(rec.defs)["Wrap"]
    for text in (filled, skel, reg, key):
        assert "(pub M);" in text and "(M);" not in text, text
    tel2 = _drive_again(tmp_path)
    assert not any("carried frozen facts" in m for m in tel2.lines), tel2.lines
    assert "(pub M);" in (tmp_path / "filled" / "src" / "wrap.rs").read_text(
        encoding="utf-8")


def _drive_again(tmp_path):
    """Second start on the tree the first start already carried."""
    from pipeline_core.adapters.layout_outcome_store import (
        load_layout_records,
        save_layout_records,
    )
    from pipeline_core.adapters.layout_registry_store import (
        load_registry,
        save_registry,
    )
    from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree
    from pipeline_core.application.generators.task2_generator import (
        StaticSkeletonTask2Generator as Gen,
    )
    from pipeline_core.domain.models import TypeRecord
    from pipeline_core.domain.private_surface import classify_private_surface

    surface = classify_private_surface([
        TypeRecord(file="wrap.rs", kind="struct", name="Wrap", is_pub=False,
                   definition=_FROZEN_WRAP),
    ], [], cfg_cluster="stable")
    tel = _Tel()
    gen = Gen.__new__(Gen)
    object.__setattr__(gen, "tel", tel)
    object.__setattr__(gen, "cargo", _Cargo([0, 0]))
    object.__setattr__(gen, "layout_sync", (
        lambda _pd, _c: surface,
        load_layout_records,
        apply_layout_to_tree,
    ))
    object.__setattr__(gen, "layout_registry", load_registry)
    object.__setattr__(gen, "save_layout_registry", save_registry)
    object.__setattr__(gen, "save_layout_records", save_layout_records)
    gen._sync_layout_into_tree(
        "[tag]", tmp_path, _Cluster(), tmp_path / "filled",
        skeleton_dir=tmp_path / ".skeleton" / "stable")
    return tel


def test_a_rolled_back_reinstall_does_not_store_the_carry(tmp_path):
    """The compile gate refused the carried text. The registry and the
    skeleton stay on the model's definition; the next start can try again."""
    from pipeline_core.adapters.layout_registry_store import load_registry

    tel = _drive(tmp_path, [0, 101])
    assert any("REVERTED" in m for m in tel.lines), tel.lines
    assert not any("carried frozen facts" in m for m in tel.lines), tel.lines
    reg = load_registry(tmp_path)["Wrap"].definition
    skel = (tmp_path / ".skeleton" / "stable" / "src" / "wrap.rs").read_text(
        encoding="utf-8")
    filled = (tmp_path / "filled" / "src" / "wrap.rs").read_text(encoding="utf-8")
    for text in (reg, skel, filled):
        assert "(M);" in text and "(pub M);" not in text, text


def test_identity_carry_leaves_the_stored_private_field(tmp_path, monkeypatch):
    """Negative control: with the carry removed, cluster start sees the
    stored text already installed and writes nothing back."""
    import pipeline_core.application.reconcile_layout as mod
    from pipeline_core.adapters.layout_registry_store import load_registry

    monkeypatch.setattr(
        mod, "carry_stored_definition", lambda item, stored: stored)
    tel = _drive(tmp_path, [0, 0])
    assert not any("carried frozen facts" in m for m in tel.lines), tel.lines
    reg = load_registry(tmp_path)["Wrap"].definition
    filled = (tmp_path / "filled" / "src" / "wrap.rs").read_text(encoding="utf-8")
    assert "(M);" in reg and "(pub M);" not in reg
    assert "(M);" in filled and "(pub M);" not in filled
