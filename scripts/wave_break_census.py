#!/usr/bin/env python3
"""Why a parallel fill route's runs open alone: the next run in dispatch
order is another route kind, or it is the same kind over a file already
in the wave.

WHY. Entry 399 of the trackers (2026-11-07) reads impl runs as 97-100 %
alone and attributes it to the wave rule "never two runs over one file"
(`fill_scheduler.py`, wave formation). The wave also needs the runs to be
CONSECUTIVE in schedule order and of one kind; when the schedule
interleaves kinds, no barrier rule can pair them. Which of the two breaks
the waves decides which change can lower alone%.

USAGE.
    python scripts/wave_break_census.py [--since YYYY-MM-DD] [--product NAME]
        [--kinds impl,file] [--events logs/events.jsonl]
Reads only. Uses phase_c.route.run events (kind, started, targets,
minutes); runs with minutes == 0 (deferred, withheld) are left out. Runs
of one (run_id, iteration) are put in dispatch order by `started`.

HOW TO INTERPRET. Per kind:
    runs         executed runs of that kind
    kind_block1  runs whose neighbours in dispatch order are both another
                 kind: alone because of the order, whatever the file rule
    same_file    runs that follow a same-kind run over a shared file: the
                 case a writes-keyed barrier would admit
    other        runs following a same-kind run over disjoint files (they
                 should already share a wave)
Decision rule: if kind_block1 dominates, alone% is set by the schedule's
order (rank interleaving kinds), and a barrier keyed on writes changes
little until the order groups a kind's runs; if same_file dominates, the
barrier rule is the lever.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--since", default="2026-10-26")
    ap.add_argument("--product", default=None)
    ap.add_argument("--kinds", default="impl,file")
    a = ap.parse_args()
    seqs: dict[tuple[str, int], list[tuple[str, str, frozenset[str]]]] = defaultdict(list)
    for line in a.events.open():
        if '"phase_c.route.run"' not in line:
            continue
        e = json.loads(line)
        if e.get("ts", "") < a.since or (a.product and e.get("product") != a.product):
            continue
        d = e.get("data") or {}
        if not d.get("minutes"):
            continue
        files = frozenset(t[0] for t in d.get("targets") or [] if t)
        seqs[(e.get("run_id") or "", int(d.get("iteration") or 0))].append(
            (d.get("started") or "", d.get("kind") or "", files))
    c: dict[str, Counter[str]] = defaultdict(Counter)
    for seq in seqs.values():
        seq.sort(key=lambda x: x[0])
        for n, (_s, kind, files) in enumerate(seq):
            c[kind]["runs"] += 1
            prev = seq[n - 1] if n else None
            nxt = seq[n + 1] if n + 1 < len(seq) else None
            if (prev is None or prev[1] != kind) and (nxt is None or nxt[1] != kind):
                c[kind]["kind_block1"] += 1
            elif prev is not None and prev[1] == kind:
                c[kind]["same_file" if prev[2] & files else "other"] += 1
    print(f"since={a.since} product={a.product or 'any'}")
    print(f"{'kind':8} {'runs':>5} {'kind_block1':>11} {'same_file':>9} {'other':>6}")
    for k in a.kinds.split(","):
        r = c.get(k, Counter())
        print(f"{k:8} {r['runs']:5d} {r['kind_block1']:11d} {r['same_file']:9d} {r['other']:6d}")


if __name__ == "__main__":
    main()
