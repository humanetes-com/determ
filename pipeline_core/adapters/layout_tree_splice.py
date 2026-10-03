"""Apply an already-validated authored layout to a LIVE (already-filled) source
tree — the layout-retrigger action's propagation step.

`author_layout` splices into the SKELETON and gates on it compiling. But the
tree cargo actually builds (`.skeleton-filled/{cluster}/`) is seeded from the
skeleton exactly once, on files that do not yet exist, and is owned from then
on. So a re-authored type definition that lands only in the skeleton is
invisible to every compile, every test, and every fill that follows: the run
reverts the referencing fns and re-fills them against the very layout the
re-author was supposed to replace.

Same locate keys, same domain primitive (`replace_surface_definitions`) as the
skeleton splice — a type definition is byte-identical across the two trees, and
a fn's key is its signature, which survives any body fill.

Writes go through the caller's :class:`TreeTxn`: this is a mechanical write
(no new information enters the tree), so the caller verifies parse — and
build, where the tree built before — and rolls the bytes back on refusal.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.tree_txn import TreeTxn
from pipeline_core.domain.scaffold_rewrite import (
    DefReplacement,
    replace_surface_definitions,
)


def apply_layout_to_tree(
    plan: dict[str, list[DefReplacement]], txn: TreeTxn,
) -> tuple[int, str]:
    """Apply `plan` (rel path → replacements) through `txn`.
    Returns `(files_written, reason)`; a non-empty reason means NOTHING was
    written — the splice is all-or-nothing, because a tree holding some of the
    new definitions and some of the old ones compiles as neither layout."""
    if not plan:
        return 0, "empty replacement plan"
    edited: dict[str, str] = {}
    for rel, reps in plan.items():
        try:
            current = txn.read(rel)
        except OSError as e:
            return 0, f"live tree file unreadable: {rel} ({e})"
        res = replace_surface_definitions(current, reps)
        if not res.replaced:
            where = ("" if res.miss_index is None
                     else f" (replacement #{res.miss_index})")
            return 0, f"could not locate a definition in {rel}{where}"
        edited[rel] = res.text
    for rel, text in edited.items():
        txn.write(rel, text)
    return len(edited), ""
