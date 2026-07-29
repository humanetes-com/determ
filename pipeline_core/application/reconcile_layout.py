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

from collections.abc import Callable, Sequence

from pipeline_core.application.author_layout import (
    LayoutComponentRecord,
    LayoutOutcome,
)
from pipeline_core.application.definition_merge import merge_live_bodies
from pipeline_core.domain.authored_layout import component_uids
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_scan import locate_top_level_item
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
    rendering of it is not the bytes on disk (see `live_definition`)."""
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
                # A reinstall replaces the DECLARATION. Any member body the
                # live tree has already implemented survives it — otherwise a
                # trait's default methods go back to `unimplemented!()` on
                # every startup and the fill loop re-earns them forever.
                merged = merge_live_bodies(authored, live_key)
                plan.setdefault(it.file, []).append(
                    DefReplacement(live_key, merged))
                notes.append(f"{it.file}: {it.name} — live tree is on a "
                             "superseded definition"
                             + (" (live member bodies preserved)"
                                if merged != authored else ""))
    return plan, notes


def live_definition(text: str, item: PrivateSurfaceItem) -> str | None:
    """The item's declaration exactly as it appears in `text`, or None.

    Sliced from the file, never taken from an extractor record: syn renders a
    declaration back with its own line wrapping AND trailing commas inside
    wrapped argument lists, so `TypeRecord.definition` is not the bytes on disk
    for any item wide enough to wrap — matching it locates nothing."""
    keyword = _ITEM_KEYWORD.get(item.kind)
    if keyword is None:
        return None
    span = locate_top_level_item(text, keyword, item.name)
    return None if span is None else text[span[0]:span[1]]


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
