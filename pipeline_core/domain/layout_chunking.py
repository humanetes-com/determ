"""Chunked layout authoring (F11 M6) — split one big connected component into
model-sized authoring chunks.

A connected component with dozens of items cannot be authored in one reply:
1:1 coverage means a single omission voids the whole attempt, and reply-length
pressure makes omissions likely precisely on the biggest components. The chunk
plan keeps design coherence by authoring the SCHEME first — every type item
(struct/enum/type-alias/trait) in one chunk, since the types must be designed
together — then the fn signatures in bounded groups, each later chunk carrying
the already-authored definitions as fixed context.

Pure planning over the component tuple; no IO, no prompt text.
"""
from __future__ import annotations

from pipeline_core.domain.private_surface import PrivateSurfaceItem, SurfaceItemKind


def plan_author_chunks(
    items: tuple[PrivateSurfaceItem, ...],
    *,
    single_shot_max: int,
    fns_per_chunk: int,
) -> tuple[tuple[int, ...], ...]:
    """The authoring chunks for one component, as index tuples into `items`
    in authoring order. Indices (not the items) because duplicate slots can be
    fully value-equal (the same trait method frozen on two impls) yet must
    keep distinct protocol identities. Components at or under
    `single_shot_max` items keep the proven one-reply path. Larger ones author
    all type items first (one scheme chunk, never split — the types reference
    each other), then fns in groups of `fns_per_chunk`."""
    if len(items) <= single_shot_max:
        return (tuple(range(len(items))),)
    types = tuple(i for i, it in enumerate(items)
                  if it.kind is not SurfaceItemKind.FN_SIG)
    fns = tuple(i for i, it in enumerate(items)
                if it.kind is SurfaceItemKind.FN_SIG)
    chunks: list[tuple[int, ...]] = []
    if types:
        chunks.append(types)
    for start in range(0, len(fns), fns_per_chunk):
        chunks.append(fns[start:start + fns_per_chunk])
    return tuple(chunks)
