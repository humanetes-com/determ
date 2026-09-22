"""Per-fn pass scheduling (determinism) — pure, no engine state."""

from __future__ import annotations

from typing import Any

from pipeline_core.domain.models import FnLocator, Frontier, Stub


def group_priority_levels(
    stubs: list[Stub], leaf_name_counts: dict[str, int] | None,
    unit_ranks: dict[FnLocator, int] | None = None,
) -> list[list[tuple[int, Stub]]]:
    """Group sorted `stubs` into barriered LEVELS for the per-fn worker pool.

    Each level is a list of `(1-based index, stub)`. The dispatch loop runs
    `min(parallelism, len(level))` workers and joins before the next level.

    Two keys, two contracts.

    Leaf-name ref-count (`unit_ranks is None`): callees before callers. Equal
    counts share a level; a count change starts a new barrier. That is the
    compile-order proxy. An unsorted input shatters into singletons
    — run `1159463-b1e352e5` iter 3 ran 5.0 h at mean concurrency 1.09
    against PIPELINE_PARALLELISM=4 because the refill tail arrived in
    source-scan order. `match_records_to_order` exists so that does not recur.

    Driver order (`unit_ranks` set): the TDD driver's final dispatch order
    as positions (`dispatch_ranks_of`), already applied by
    `rank_stubs_by_blocker_counts` before this call. They are not a
    compile-order proxy and never tie between locators. Splitting on every
    distinct count was the leaf-first rule applied to the wrong key: run
    `346231-54bc1489` iter 1's 12-unit batch became 8 levels, the first
    five singletons, and after file-first the per-fn phase ran at mean LLM
    concurrency 0.99 for 3 h (`construct` 44 min, then `construct_from_boxed`
    61 min, then `ensure.rs::write_str` 13 min in another file, then
    `context` 49 min). One level: the engine's pool drains a ranked queue,
    so the first `parallelism` starts are the driver's head and a
    free worker takes the next without waiting for the slowest peer.
    Ranking still decides start ORDER; it does not decide who waits.

    Without `unit_ranks` the engine re-sorts leaf-first after `take_batch`,
    which undoes constructor-first ranking: run `155810-cbb3b9fc` iter 1
    dispatched `fmt` first and `construct_from_adhoc` last even though both
    were in the batch, because `fmt` is a high-frequency name. Pure: no
    engine/closure state.
    """
    if not stubs:
        return []
    if unit_ranks is not None:
        return [[(i, r) for i, r in enumerate(stubs, 1)]]
    levels: list[list[tuple[int, Stub]]] = []
    prev_key: object = object()
    for i, r in enumerate(stubs, 1):
        key = leaf_name_counts.get(r.fn_name, 0) if leaf_name_counts else 0
        if key != prev_key:
            levels.append([])
            prev_key = key
        levels[-1].append((i, r))
    return levels


def rank_stubs_by_blocker_counts(
    stubs: list[Stub], ranks: dict[FnLocator, int],
) -> list[Stub]:
    """`stubs` descending by blocker count, then file/fn/container for ties.

    The input to `group_priority_levels` when `unit_ranks` is set. Ranking
    is the queue order of the single pool that path returns — not a
    partition into count-change barriers."""
    return sorted(
        stubs,
        key=lambda s: (-ranks.get(s.key, 0), s.file, s.fn_name, s.container),
    )


def match_records_to_order(
    ordered: list[FnLocator], records: list[Stub],
) -> list[Stub]:
    """Reorder `records` to follow `ordered` (a leaf-first key sequence),
    taking same-key records in their original relative order.

    Every record batch that feeds `group_priority_levels` MUST pass through a
    leaf-first ordering first: that function opens a new barriered LEVEL at
    every ref-count change, so an unsorted segment shatters into
    near-singleton levels and the per-level worker pool dispatches alone.
    Measured on run `1159463-b1e352e5` iter 3 (2026-08-09, cluster `stable`):
    the batch was 33 behavioral re-fills appended in source-scan order, and
    the per-fn phase ran 5.0 h at mean concurrency 1.09 against
    PIPELINE_PARALLELISM=4 — 91 % of it strictly serial, on a level
    structure that leaf-first ordering would have grouped as 12 levels
    (4 of them holding 4-7 units each). Pure: no engine state."""
    remaining: dict[FnLocator, list[Stub]] = {}
    for r in records:
        remaining.setdefault(r.key, []).append(r)
    out: list[Stub] = []
    for key in ordered:
        bucket = remaining.get(key)
        if bucket:
            out.append(bucket.pop(0))
    return out


def leaf_first_group_order(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort groups so leaf groups (fewest cross-group symbol overlaps) come first.

    A group sharing many `target_symbols` with others depends on more "other"
    groups' work, so should be filled later. Ties broken by group name for
    determinism. Pure: no engine state.
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
    stubs: list[FnLocator],
    name_counts: dict[str, int] | None,
) -> list[FnLocator]:
    """Sort stubs so the most-referenced fns come first (likely call-graph
    leaves — utility fns relied on by many others). Public-API entry points end
    up last, where they can see real helper bodies on disk.

    `name_counts` is the `{fn_name: ref_count}` map written during the pre-strip
    bake step. When empty/None, falls back to deterministic order (file, fn)."""
    if not name_counts:
        return sorted(stubs)
    scores: dict[FnLocator, int] = {}
    for s in stubs:
        _, fn = s
        scores[s] = name_counts.get(fn, 0) if fn else 0
    # Higher refs = more depended on = fill earlier (leaf-first).
    return sorted(stubs, key=lambda s: (-scores.get(s, 0), s[0], s[1]))


def order_frontier(
    frontier: Frontier,
    leaf_name_counts: dict[str, int] | None,
    panic_depths: dict[FnLocator, int] | None = None,
) -> list[tuple[FnLocator, set[str]]]:
    """Order frontier entries: shallowest panic depth first, then
    most-callers, then alphabetical (file, fn) for reproducibility.

    `panic_depths` (optional) maps the same keys to integer panic stack depths.
    Absent → all same depth, the secondary sort wins. Most-referenced fn (from
    `leaf_name_counts`) breaks final ties so the pre-strip leaf-order metadata
    still influences scheduling."""
    panic_depths = panic_depths or {}
    leaf_name_counts = leaf_name_counts or {}
    items = list(frontier.items())

    def _key(item: tuple[FnLocator, set[str]]) -> tuple[int, int, int, str, str]:
        (file_rel, fn_name), tests = item
        return (
            panic_depths.get(FnLocator(file_rel, fn_name), 0),  # shallowest first
            -len(tests),                                # most tests first
            -leaf_name_counts.get(fn_name, 0),          # most-referenced first
            file_rel, fn_name,                          # alphabetical reproducible
        )
    return sorted(items, key=_key)
