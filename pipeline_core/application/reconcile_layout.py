"""Make the built tree hold the layout that was actually authored.

`.skeleton-filled/{cluster}/` is seeded from `.skeleton/{cluster}/` once, for
files that do not yet exist, and is owned from then on — the fill loop rewrites
bodies in it and must never have them clobbered. That one-way seed is correct
for bodies and wrong for the private LAYOUT: a mid-run re-author edits the
skeleton, and its new type/impl blocks never reach the tree cargo compiles. The
run then reverts the referencing fns and re-fills them against the very layout
the re-author replaced, while the build keeps failing for the original reason.

This module restores the missing half of that seed. It propagates only the
non-fn authored items — types, aliases, traits, and the impl blocks the model
wrote alongside them. Fn items are deliberately excluded: their stored key is a
signature, not a whole item, and their bodies belong to the fill loop.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from pipeline_core.application.author_layout import (
    LayoutComponentRecord,
    LayoutOutcome,
)
from pipeline_core.application.definition_merge import merge_live_bodies
from pipeline_core.domain.authored_layout import (
    AuthoredItem,
    carry_stored_definition,
    component_uids,
)
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_scan import locate_item_declaration
from pipeline_core.domain.scaffold_rewrite import (
    DefReplacement,
    find_definition_span,
)

_ITEM_KEYWORD = {
    SurfaceItemKind.STRUCT: "struct",
    SurfaceItemKind.ENUM: "enum",
    SurfaceItemKind.TYPE_ALIAS: "type",
    SurfaceItemKind.TRAIT: "trait",
    SurfaceItemKind.PUB_FIELDS: "struct",
}


def adopt_registry_definitions(
    records: Sequence[LayoutComponentRecord],
    registry: Mapping[str, AuthoredItem],
) -> tuple[list[LayoutComponentRecord], list[str]]:
    """`records` with every definition the crate-wide registry has already fixed
    substituted in, plus a note per substitution.

    The registry holds ONE definition per private item — whichever cfg-cluster
    reached it first — because the merged crate has exactly one. Adoption is
    meant to make a later cluster reuse it (`adopt_component`), and when that
    works there is nothing here to do: the record and the registry agree and
    every substitution is a no-op.

    It does not always work. A cluster authored before the registry held an item
    (or one whose adoption fell through and re-authored the component) persists a
    SECOND definition of a type the crate has already fixed, and that record then
    outranks the registry forever, because nothing reads the two together.
    Measured on `anyhow`: `stable` and `nightly` both recorded AUTHORED for the
    same 35-item component, all 35 uids present in the registry, and 32 of the
    definitions differed. Two of them are why `nightly` could not compile at all:

        Error      stable/registry: + `unsafe impl Send`/`Sync`   nightly: absent
        ErrorImpl  stable/registry: {vtable, backtrace, _error}   nightly: {vtable, _data}

    The first is six `E0277 NonNull<ErrorImpl> cannot be sent between threads`
    in the oracle's own test files; the second is what made 61 of 121 donor
    bodies unseedable. Neither is repairable by any fill, because the fix is a
    declaration and the fill loop writes bodies.

    So the registry wins over a per-cluster record. That is the rule the store
    was built to express, applied at the point the tree is written rather than
    only at authoring time. The cost is a cluster that genuinely needs its own
    shape for a shared uid cannot have one — but a cfg-specific item is a
    different item with a different uid, and a shared uid with two shapes is the
    contradiction that stops the crate compiling either way.

    Records are rebuilt, never mutated: the caller's copy is the store's."""
    out: list[LayoutComponentRecord] = []
    notes: list[str] = []
    for rec in records:
        if rec.outcome is not LayoutOutcome.AUTHORED or not rec.defs:
            out.append(rec)
            continue
        swapped: list[tuple[str, str]] = []
        for uid, stored in rec.defs:
            fixed = registry.get(uid)
            # Whitespace-normalised: the two texts are rendered by the same
            # extractor but not necessarily wrapped identically, and a rewrap
            # is not a redefinition.
            if fixed is None or " ".join(fixed.definition.split()) == \
                    " ".join(stored.split()):
                swapped.append((uid, stored))
                continue
            swapped.append((uid, fixed.definition))
            notes.append(f"{uid} — this cluster recorded its own definition of "
                         "an item the crate has already fixed; adopting the "
                         "registry's")
        out.append(LayoutComponentRecord(
            names=rec.names, outcome=rec.outcome, defs=tuple(swapped)))
    return out, notes


def plan_layout_reconciliation(
    surface: PrivateSurface,
    records: Sequence[LayoutComponentRecord],
    read_live: Callable[[str], str | None],
) -> tuple[dict[str, list[DefReplacement]], list[str]]:
    """`(per-file replacements, notes)` bringing the live tree's authored items
    up to the definitions the layout store records as AUTHORED.

    An item already present in the live tree contributes nothing, so a healthy
    tree plans zero work and this is a no-op on every normal iteration. The
    locate side is SLICED from the live file — the skeleton's text is what we
    are installing, so it cannot also be what we search for, and an extractor's
    rendering of it is not the bytes on disk (see `live_definition`).

    The text matched and installed is the stored definition after
    `carry_stored_definition`, not the bytes the store happens to hold. A
    registry written before frozen repr and visibility were carried still has
    the model's text; matching that text treats a private field as installed
    and reinstalls it over a tree that had already been spliced with the
    carried one."""
    plan: dict[str, list[DefReplacement]] = {}
    notes: list[str] = []
    for rec in records:
        if rec.outcome is not LayoutOutcome.AUTHORED or not rec.defs:
            continue
        authored_by_uid = dict(rec.defs)
        for comp in _components_for(surface, rec.names):
            for uid, it in zip(component_uids(comp), comp, strict=True):
                if it.kind is SurfaceItemKind.FN_SIG:
                    continue          # bodies are the fill loop's, not ours
                authored = authored_by_uid.get(uid)
                if not authored:
                    continue
                # Match and install the carried text. The store may still hold
                # the model's definition; using that for the span check leaves
                # a dropped frozen visibility in place for the life of the tree.
                authored = carry_stored_definition(it, authored)
                text = read_live(it.file)
                if text is None:
                    # Never silent: an unreadable path is indistinguishable
                    # from a healthy tree in the plan (both produce no work),
                    # and a path resolved against the wrong base reads as
                    # "nothing to reinstall" on every item forever.
                    notes.append(f"{it.file}: {it.name} — live file could not "
                                 "be read; nothing reinstalled")
                    continue
                if find_definition_span(text, authored) is not None:
                    continue          # already installed
                live_key = live_definition(text, it)
                if live_key is None:
                    notes.append(f"{it.file}: {it.name} — superseded, but the "
                                 "declaration could not be located in the live "
                                 "tree; leaving it alone")
                    continue
                # The item as the file holds it: its outer doc comments are
                # part of it. The authored text carries them, so replacing
                # only the declaration below them added one more copy of the
                # doc on every reinstall.
                live_key = _with_outer_docs(text, live_key)
                # A reinstall replaces the DECLARATION. Any member body the
                # live tree has already implemented survives it — otherwise a
                # trait's default methods go back to `unimplemented!()` on
                # every startup and the fill loop re-earns them forever.
                merged = merge_live_bodies(authored, live_key)
                if _ws(merged) == _ws(live_key):
                    # Installed: what a reinstall would write is what is
                    # there. Compared against the AUTHORED text alone, a tree
                    # whose member bodies the fill loop has written never
                    # matches, and the item was "superseded" on every launch
                    # (`kind.rs` `AdhocKind`/`TraitKind` on every stable and
                    # nightly start in the kept logs; run `3893442-cb547388`,
                    # 2026-09-19, found its doc comment seven times over).
                    continue
                plan.setdefault(it.file, []).append(
                    DefReplacement(live_key, merged))
                notes.append(f"{it.file}: {it.name} — live tree is on a "
                             "superseded definition"
                             + (" (live member bodies preserved)"
                                if merged != authored else ""))
    return plan, notes


def carry_registry(
    surface: PrivateSurface, registry: Mapping[str, AuthoredItem],
) -> dict[str, AuthoredItem]:
    """``registry`` with each stored definition carried against the surface.

    Items this surface does not declare are left as stored: another cluster's
    sync carries those. Unchanged items keep their object, so a caller can
    tell a rewrite from an identity by comparing definitions."""
    by_uid = _surface_by_uid(surface)
    out = dict(registry)
    for uid, item in registry.items():
        it = by_uid.get(uid)
        if it is None:
            continue
        text = carry_stored_definition(it, item.definition)
        if text != item.definition:
            out[uid] = replace(item, definition=text)
    return out


def carried_locate_keys(
    surface: PrivateSurface,
    records: Sequence[LayoutComponentRecord],
    read_live: Callable[[str], str | None],
) -> tuple[list[LayoutComponentRecord], bool]:
    """Records whose locate keys are the carried definition, once that text
    is what ``read_live`` holds.

    A key is updated only when the file contains the carried definition.
    Updating a key the skeleton does not hold makes the next re-author miss
    the item. Fn keys are never carried (they are signatures, not layouts).
    The bool is whether any key changed."""
    by_uid = _surface_by_uid(surface)
    out: list[LayoutComponentRecord] = []
    changed = False
    for rec in records:
        defs: list[tuple[str, str]] = []
        for uid, stored in rec.defs:
            it = by_uid.get(uid)
            text = stored
            if it is not None:
                carried = carry_stored_definition(it, stored)
                live = read_live(it.file)
                if (live is not None
                        and find_definition_span(live, carried) is not None):
                    text = carried
            if text != stored:
                changed = True
            defs.append((uid, text))
        out.append(LayoutComponentRecord(
            names=rec.names, outcome=rec.outcome, defs=tuple(defs)))
    return out, changed


def _surface_by_uid(
    surface: PrivateSurface,
) -> dict[str, PrivateSurfaceItem]:
    out: dict[str, PrivateSurfaceItem] = {}
    for comp in surface.components:
        for uid, it in zip(component_uids(comp), comp, strict=True):
            out[uid] = it
    return out


def live_definition(text: str, item: PrivateSurfaceItem) -> str | None:
    """The item's declaration exactly as it appears in `text`, or None.

    Sliced from the file, never taken from an extractor record: syn renders a
    declaration back with its own line wrapping AND trailing commas inside
    wrapped argument lists, so `TypeRecord.definition` is not the bytes on disk
    for any item wide enough to wrap — matching it locates nothing.

    Searched at any module depth, not just the file's top level: a surface item
    is whatever the crate declared, and a crate is free to declare one inside an
    inline `mod` (`anyhow`'s `mod ext` holds `StdError` and `Sealed`). Scoped to
    depth 0 this returned None for them, which every caller reads as "not in the
    live tree" — see `locate_item_declaration`."""
    keyword = _ITEM_KEYWORD.get(item.kind)
    if keyword is None:
        return None
    span = locate_item_declaration(text, keyword, item.name)
    return None if span is None else text[span[0]:span[1]]


def _ws(text: str) -> str:
    return " ".join(text.split())


def _with_outer_docs(text: str, decl: str) -> str:
    """`decl` (a slice of `text`) extended back over the `///` lines directly
    above it — the item's outer doc comments, which `live_definition` leaves
    out because it starts at the item's attributes."""
    start = text.find(decl)
    if start < 0:
        return decl
    lines_before = text[:start].split("\n")
    # The line `decl` starts on is lines_before[-1] (its indentation, if any).
    head = lines_before[:-1]
    n = 0
    while n < len(head) and head[len(head) - 1 - n].lstrip().startswith("///"):
        n += 1
    if n == 0:
        return decl
    doc_start = start - len(lines_before[-1]) - sum(
        len(ln) + 1 for ln in head[len(head) - n:])
    return text[doc_start:start + len(decl)]


def _components_for(
    surface: PrivateSurface, names: frozenset[str],
) -> list[tuple[PrivateSurfaceItem, ...]]:
    """The surface's connected components restricted to `names` — the same
    grouping (and therefore the same uid numbering) the original splice used."""
    out: list[tuple[PrivateSurfaceItem, ...]] = []
    for comp in surface.components:
        sel = tuple(i for i in comp if i.name in names)
        if len(sel) == len(comp) and sel:
            out.append(sel)
    return out
