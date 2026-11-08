#!/usr/bin/env python3
"""How many kept bodies a (function, red test) pair takes before the test
goes green, and how many the pairs that never went green had.

WHY. A plan of 2026-11-07 runs every task 2a call at a medium reasoning
level and asks a (function, test) pair once at the maximum level when its
medium bodies are not turning the test green. "Not turning it green" needs a
count K of landed bodies after which the record says another landing at the
same level does not pay. The count is read here from the fill's own route
rows, never chosen: every recorded run so far ran at one level (Qwen3.8 at
xhigh, Muse at high), so this is the landings-before-green of that level and
must be read again on the first medium lane.

USAGE.
    python scripts/landings_before_green.py [--events logs/events.jsonl]
        [--since YYYY-MM-DD] [--product NAME]
Reads only `phase_c.route.run` events (fields kept, target_tests,
newly_green, still_red, iteration, run_id). A landing of (u, t) is a route
row that kept unit u while t was among its target tests, its newly green or
its still red. A pair's count resets when t goes green. Counted per run_id
(one process), because the ask ledger is keyed per oracle and a relaunch on
the same oracle keeps it; read `--lane oracle` style chaining as future work.

HOW TO INTERPRET.
    green_after N   pairs whose test went green on their N-th landing
    red_at_end N    pairs still red at the run's last row after N landings
Decision rule: K is the smallest N such that the green_after mass above N
is small against the red_at_end mass at or above N, i.e. one more landing at
the same level has rarely paid on the record. Report the cumulative share
of greens reached by N (cum_green%).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--since", default="2026-10-01")
    ap.add_argument("--product", default=None)
    a = ap.parse_args()
    rows: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for line in a.events.open():
        if '"phase_c.route.run"' not in line:
            continue
        e = json.loads(line)
        if e.get("ts", "") < a.since or (a.product and e.get("product") != a.product):
            continue
        rows[e.get("run_id") or ""].append((e["ts"], e.get("data") or {}))
    green_after: Counter[int] = Counter()
    red_at_end: Counter[int] = Counter()
    runs = 0
    for _run, seq in rows.items():
        seq.sort(key=lambda x: x[0])
        count: dict[tuple[str, str], int] = defaultdict(int)
        runs += 1
        for _ts, d in seq:
            kept = {f"{k[0]}::{k[1]}" for k in d.get("kept") or [] if len(k) >= 2}
            if not kept:
                continue
            green = set(d.get("newly_green") or [])
            tests = set(d.get("target_tests") or []) | green | set(d.get("still_red") or [])
            for u in kept:
                for t in tests:
                    count[(u, t)] += 1
                    if t in green:
                        green_after[count[(u, t)]] += 1
                        count[(u, t)] = 0
            for (u, t) in list(count):
                if t in green and (u, t) in count and u not in kept:
                    count[(u, t)] = 0
        for n in count.values():
            if n:
                red_at_end[n] += 1
    total_g = sum(green_after.values())
    print(f"runs={runs} greens={total_g} red_pairs_at_end={sum(red_at_end.values())}")
    print(f"{'N':>3} {'green_after':>11} {'cum_green%':>10} {'red_at_end':>10}")
    cum = 0
    for n in sorted(set(green_after) | set(red_at_end)):
        cum += green_after.get(n, 0)
        print(f"{n:3d} {green_after.get(n, 0):11d} {100 * cum / total_g if total_g else 0:9.1f}% "
              f"{red_at_end.get(n, 0):10d}")


if __name__ == "__main__":
    main()
