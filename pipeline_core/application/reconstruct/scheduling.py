"""Per-fn pass scheduling (R11.3 determinism) — pure, no engine state."""

from __future__ import annotations

from typing import Any

from pipeline_core.domain.models import Frontier, Stub


def group_priority_levels(
    stubs: list[Stub], leaf_name_counts: dict[str, int] | None,
) -> list[list[tuple[int, Stub]]]:
    """Group leaf-first-sorted `stubs` into contiguous equal-ref-count LEVELS.

    Each level is a list of `(1-based index, stub)`. `stubs` is already sorted
    descending by ref-count, so equal-priority stubs are contiguous; a key change
    starts a new level. The dispatch loop runs each level with a barrier between
    them, so a more-referenced (callee) level always completes before the level
    that depends on it — every fill sees real sibling bodies in a scheduling-
    independent order (R11.3 determinism). Pure: no engine/closure state.
    """
    levels: list[list[tuple[int, Stub]]] = []
    prev_key: object = object()
    for i, r in enumerate(stubs, 1):
        key = leaf_name_counts.get(r.fn_name, 0) if leaf_name_counts else 0
        if key != prev_key:
            levels.append([])
            prev_key = key
        levels[-1].append((i, r))
    return levels


def leaf_first_group_order(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort groups so leaf groups (fewest cross-group symbol overlaps) come first.

    A group sharing many `target_symbols` with others depends on more "other"
    groups' work, so should be filled later. Ties broken by group name for
    determinism (R11.3). Pure: no engine state.
    """
    all_other_syms: list[set[str]] = [set(g.get("target_symbols", []) or []) for g in groups]

    def overlap_count(idx: int) -> int:
        mine = set(groups[idx].get("target_symbols", []) or [])
        if not mine:
            return 0
        count = 0
        for j, others in enumerate(all_other_syms):
            if j == idx:
                continue
            count += len(mine & others)
        return count

    indexed = list(range(len(groups)))
    indexed.sort(key=lambda i: (overlap_count(i), str(groups[i].get("name", ""))))
    return [groups[i] for i in indexed]


def leaf_first_fn_order(
    stubs: list[tuple[str, str]],
    name_counts: dict[str, int] | None,
) -> list[tuple[str, str]]:
    """Sort stubs so the most-referenced fns come first (likely call-graph
    leaves — utility fns relied on by many others). Public-API entry points end
    up last, where they can see real helper bodies on disk.

    `name_counts` is the `{fn_name: ref_count}` map written during the pre-strip
    bake step. When empty/None, falls back to deterministic order (file, fn)."""
    if not name_counts:
        return sorted(stubs)
    scores: dict[tuple[str, str], int] = {}
    for s in stubs:
        _, fn = s
        scores[s] = name_counts.get(fn, 0) if fn else 0
    # Higher refs = more depended on = fill earlier (leaf-first).
    return sorted(stubs, key=lambda s: (-scores.get(s, 0), s[0], s[1]))


def order_frontier(
    frontier: Frontier,
    leaf_name_counts: dict[str, int] | None,
    panic_depths: dict[tuple[str, str], int] | None = None,
) -> list[tuple[tuple[str, str], set[str]]]:
    """Order frontier entries by D2's rule: shallowest panic depth first, then
    most-callers, then alphabetical (file, fn) for reproducibility.

    `panic_depths` (optional) maps the same keys to integer panic stack depths.
    Absent → all same depth, the secondary sort wins. Most-referenced fn (from
    `leaf_name_counts`) breaks final ties so the pre-strip leaf-order metadata
    still influences scheduling."""
    panic_depths = panic_depths or {}
    leaf_name_counts = leaf_name_counts or {}
    items = list(frontier.items())

    def _key(item: tuple[tuple[str, str], set[str]]) -> tuple[int, int, int, str, str]:
        (file_rel, fn_name), tests = item
        return (
            panic_depths.get((file_rel, fn_name), 0),  # shallowest first
            -len(tests),                                # most tests first
            -leaf_name_counts.get(fn_name, 0),          # most-referenced first
            file_rel, fn_name,                          # alphabetical reproducible
        )
    return sorted(items, key=_key)
