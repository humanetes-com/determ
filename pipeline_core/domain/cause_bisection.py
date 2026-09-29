"""Which of several landed changes caused an observed move.

The sweep's site-regression block used to revert every landing whose
attributed set touched a moved test. Run `273278-22529e44` (2026-09-29,
cluster `stable`) reverted ten, including `src/lib.rs::Ok` (12 tests gained
on its own row) and both `src/kind.rs` units, and the score fell from 37 to
19. Attribution says which tests a body answers for; it does not say which
body moved a test. Only a measurement does.

`localize` walks halves: undo one half, re-measure, and recurse into the
half whose undo moves the observation back. Both halves moving it back means
each is sufficient on its own, and the walk continues in both. Neither
moving it back means the cause is the conjunction of the two halves, and
that set is returned whole. A walk without those two branches reverts a
half that does not hold the cause, or reverts nothing.

The cost is two re-measurements per level, ~2·log2(n), against a revert
that destroyed 18 passing tests.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence


def localize[T](
    candidates: Sequence[T], moves_back: Callable[[Sequence[T]], bool],
) -> tuple[T, ...]:
    """The subset of `candidates` whose undo moves the observation back.

    `moves_back(subset)` undoes `subset` alone on the current tree,
    re-measures, reports whether the observation moved back, and leaves the
    tree as it found it. The caller has established that undoing all of
    `candidates` moves it back (or accepts that the whole set is returned
    when nothing narrower does); a single candidate is returned without a
    measurement."""
    items = tuple(candidates)
    if len(items) <= 1:
        return items
    mid = len(items) // 2
    first, second = items[:mid], items[mid:]
    a = moves_back(first)
    b = moves_back(second)
    if a and b:
        return localize(first, moves_back) + localize(second, moves_back)
    if a:
        return localize(first, moves_back)
    if b:
        return localize(second, moves_back)
    return items
