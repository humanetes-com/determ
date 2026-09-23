"""Replay a recorded fill schedule's ORDER under the live walk and under a
strongly-connected-component walk.

WHY. Run `484518-91681c1a` (2026-09-23), fill 5: the sweep named
`src/ptr.rs::new` as the first failure of all 141 failing tests and the
per-fn run holding it ranked level 0, yet the schedule opened two level-2
formatter pairs first (no stub, no aimed test, 20 minutes, flat). The
blocks relation among the level-0/1 runs was one cycle of 11 runs, so the
walk's "first run whose blockers are all placed" could only pick the runs
nothing blocks — the pairs — before falling back to sort order. This script
recomputes both orders from the `phase_c.fill.schedule` event so the claim
and its negative control are one command.

USAGE.
    python scripts/schedule_order_replay.py <run-id-prefix> [fill-index]
Reads logs/events.jsonl. fill-index is the 0-based index among that run's
schedule events (default: the last). Offline, no GPU, no cargo.

HOW TO INTERPRET.
    CURRENT  — the first runs of the live walk, rebuilt from each run's
               recorded level/rank/position and `blocked_by`. It must match
               the recorded `[phase-c/schedule]` order; if it does not, the
               replay is not the live rule and nothing below is evidence.
    SCC      — the same inputs ordered over strongly connected components
               of the blocks graph (components topologically, ties by the
               best member's sort key; members by their own sort key).
    components — sizes of the components. One large component is the
               stub-heavy-tree shape in which the live walk promotes the
               unblockable runs.
    Decision: if CURRENT opens a run of a higher level than SCC's first run,
    the fill was mis-ordered by the cycle fallback.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    prefix = sys.argv[1]
    which = int(sys.argv[2]) if len(sys.argv) > 2 else -1
    events = [json.loads(line) for line in open("logs/events.jsonl")
              if prefix in line[:400]]
    sched = [e for e in events if e.get("event") == "phase_c.fill.schedule"]
    runs = sched[which]["data"]["runs"]
    labels = [r["label"] for r in runs]
    key = {r["label"]: (r.get("level"), r.get("rank"), i)
           for i, r in enumerate(runs)}
    # `blocked_by` can name a run the schedule deferred out of `runs`; a
    # deferred run is never placed, so it cannot hold anything in either walk.
    by = {r["label"]: set(r.get("blocked_by") or []) & set(labels)
          for r in runs}
    order = sorted(labels, key=lambda lab: key[lab])

    placed: list[str] = []
    pending = list(order)
    while pending:
        pick = next((lab for lab in pending if not (by[lab] - set(placed))),
                    None) or pending[0]
        placed.append(pick)
        pending.remove(pick)
    print("CURRENT   :", placed[:4])

    sys.setrecursionlimit(10000)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    comps: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in (x for x in labels if v in by[x]):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            comps.append(comp)

    for v in labels:
        if v not in index:
            strong(v)
    cid = {v: i for i, c in enumerate(comps) for v in c}
    cby = {i: {cid[b] for v in c for b in by[v] if cid[b] != i}
           for i, c in enumerate(comps)}
    ckey = {i: min(key[v] for v in c) for i, c in enumerate(comps)}
    out: list[str] = []
    done: set[int] = set()
    todo = sorted(range(len(comps)), key=lambda i: ckey[i])
    while todo:
        pick_c = next(i for i in todo if not (cby[i] - done))
        done.add(pick_c)
        todo.remove(pick_c)
        out += sorted(comps[pick_c], key=lambda lab: key[lab])
    print("SCC       :", out[:4])
    print("components:", sorted((len(c) for c in comps), reverse=True))


if __name__ == "__main__":
    main()
