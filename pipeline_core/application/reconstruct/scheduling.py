"""Per-fn pass scheduling (R11.3 determinism) — pure, no engine state."""

from __future__ import annotations

from typing import Any


def group_priority_levels(
    stubs: list[dict[str, Any]], leaf_name_counts: dict[str, int] | None,
) -> list[list[tuple[int, dict[str, Any]]]]:
    """Group leaf-first-sorted `stubs` into contiguous equal-ref-count LEVELS.

    Each level is a list of `(1-based index, stub)`. `stubs` is already sorted
    descending by ref-count, so equal-priority stubs are contiguous; a key change
    starts a new level. The dispatch loop runs each level with a barrier between
    them, so a more-referenced (callee) level always completes before the level
    that depends on it — every fill sees real sibling bodies in a scheduling-
    independent order (R11.3 determinism). Pure: no engine/closure state.
    """
    levels: list[list[tuple[int, dict[str, Any]]]] = []
    prev_key: object = object()
    for i, r in enumerate(stubs, 1):
        key = leaf_name_counts.get(r["fn_name"], 0) if leaf_name_counts else 0
        if key != prev_key:
            levels.append([])
            prev_key = key
        levels[-1].append((i, r))
    return levels
