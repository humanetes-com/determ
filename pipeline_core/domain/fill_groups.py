"""Coupled sub-group partition for the file-first fill route.

A file whose stub+re-fill count exceeds the single-prompt bound used to be
skipped wholesale — fragmenting exactly the coupled families (a vtable built
by one fn and dispatched through by its siblings) back to per-fn fills at the
moment they are most-stubbed. Instead: partition the file's impl blocks into
groups that must be designed together, each within the prompt bound.

Coupling signal: two blocks sharing ANY attributed frontier test are welded —
the attribution IS the evidence that one validation run spans their members
jointly, so filling them apart guarantees each is judged against siblings it
cannot control. Blocks are never split (container-scoped routing owns them
whole). Deterministic; no LLM, no name heuristics.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BlockGroupInput:
    """One impl block's grouping facts. ``idx`` is the caller's index into its
    own block list (opaque here, returned in the partition)."""

    idx: int
    fn_count: int
    tests: frozenset[str]


def coupled_fill_groups(
    blocks: list[BlockGroupInput],
    max_fns: int,
) -> list[list[int]]:
    """Partition ``blocks`` into joint-fill groups of whole blocks, each with
    a total ``fn_count`` ≤ ``max_fns`` where possible.

    Test-welded components are kept together when they fit; an oversized
    component is split by greedy shared-test connectivity (each group grows by
    the block sharing most tests with it, and closes rather than absorb a
    block sharing none — a coherent chain segment beats a padded group). A
    single block larger than ``max_fns`` comes out as its own group; the
    caller decides its fate. Ordering is deterministic: heaviest component
    first, ties by lowest block index."""
    if not blocks:
        return []
    by_idx = {b.idx: b for b in blocks}
    parent = {b.idx: b.idx for b in blocks}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    idxs = sorted(by_idx)
    for i, a in enumerate(idxs):
        for b in idxs[i + 1:]:
            if by_idx[a].tests and by_idx[a].tests & by_idx[b].tests:
                union(a, b)

    comps: dict[int, list[int]] = {}
    for i in idxs:
        comps.setdefault(find(i), []).append(i)
    comp_list = sorted(
        comps.values(),
        key=lambda c: (-sum(by_idx[i].fn_count for i in c), min(c)))

    groups: list[list[int]] = []
    for comp in comp_list:
        if sum(by_idx[i].fn_count for i in comp) <= max_fns:
            groups.append(sorted(comp))
            continue
        remaining = set(comp)
        while remaining:
            seed = -1
            seed_rank: tuple[int, int, int] | None = None
            for i in remaining:
                degree = sum(1 for j in remaining
                             if j != i and by_idx[i].tests & by_idx[j].tests)
                rank = (degree, by_idx[i].fn_count, -i)
                if seed_rank is None or rank > seed_rank:
                    seed_rank, seed = rank, i
            group = [seed]
            remaining.discard(seed)
            gtests = set(by_idx[seed].tests)
            gcount = by_idx[seed].fn_count
            while True:
                nxt = -1
                nxt_rank: tuple[int, int, int] | None = None
                for i in remaining:
                    if gcount + by_idx[i].fn_count > max_fns:
                        continue
                    rank = (len(by_idx[i].tests & gtests),
                            by_idx[i].fn_count, -i)
                    if nxt_rank is None or rank > nxt_rank:
                        nxt_rank, nxt = rank, i
                if nxt_rank is None or nxt_rank[0] == 0:
                    # nothing fits, or the best candidate shares no test with
                    # the group — close a coherent segment rather than pad it
                    break
                group.append(nxt)
                remaining.discard(nxt)
                gtests |= by_idx[nxt].tests
                gcount += by_idx[nxt].fn_count
            groups.append(sorted(group))
    return groups
