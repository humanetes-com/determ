"""Probe: how much of a crate's private surface is welded into one t1.5
component by the SAME-NAME edge in `connected_components`, versus by genuine
type references?

`connected_components` adds an undirected edge between every pair of items
sharing a bare `name` ("a helper fn sig and the type it shares a name with
belong together"). Bare names are not unique across impl blocks — `source`
appears once per impl that has one — so that edge can weld otherwise-unrelated
sub-graphs into one atomic authoring unit. This prints both partitions so the
difference is measured, not assumed.

    .venv/bin/python tools/probe_layout_components.py [cfg_cluster]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.private_surface_store import (  # noqa: E402
    load_private_surface,
)

_PROJECT = Path("tmp/anyhow")


def _components_without_name_edge(
    items: tuple, ) -> tuple[tuple, ...]:
    """`connected_components` with ONLY the reference edges (no same-name
    union), so the two partitions can be compared item-for-item."""
    by_name: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        by_name.setdefault(it.name, []).append(i)
    adj: dict[int, set[int]] = {i: set() for i in range(len(items))}
    for i, it in enumerate(items):
        for ref in it.references:
            for j in by_name.get(ref, ()):
                if j != i:
                    adj[i].add(j)
                    adj[j].add(i)
    seen: set[int] = set()
    comps = []
    for start in range(len(items)):
        if start in seen:
            continue
        stack, comp = [start], []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.append(n)
            stack.extend(adj[n] - seen)
        comps.append(tuple(items[i] for i in sorted(comp)))
    return tuple(comps)


def main() -> None:
    cluster = sys.argv[1] if len(sys.argv) > 1 else "stable"
    surface = load_private_surface(_PROJECT, cluster)
    items = surface.replaceable
    print(f"cluster={cluster} replaceable={len(items)}")

    with_name = surface.components
    without = _components_without_name_edge(items)

    def _show(label: str, comps: tuple) -> None:
        sizes = sorted((len(c) for c in comps), reverse=True)
        print(f"\n{label}: {len(comps)} component(s), sizes={sizes}")
        for c in sorted(comps, key=len, reverse=True)[:3]:
            files = sorted({i.file for i in c})
            print(f"  size={len(c):3d} files={files}")

    _show("WITH same-name edge (what t1.5 authors)", with_name)
    _show("reference edges ONLY", without)

    biggest = max(with_name, key=len)
    print(f"\nlargest authored-together unit: {len(biggest)} items, "
          f"{len(sorted({i.file for i in biggest}))} file(s) — "
          "all authored atomically in one LLM exchange, committed only if the "
          "whole rewritten skeleton compiles.")


if __name__ == "__main__":
    main()
