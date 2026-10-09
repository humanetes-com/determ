#!/usr/bin/env python3
"""Starved slice units: a unit on a red test's executed slice that no OPENED
fill run has written since that test's failure last moved, K fills running.

WHY THIS EXISTS
  Every per-test predicate over a wide owner set is satisfied by writing the
  wrong unit: on run 14141-9a904822 every red test had SOME owner written by an
  opened run in every fill, while src/ensure.rs::render (the one wrong body
  behind 16 of the 21 leftovers) went unwritten from fill 3 to fill 8 — no
  candidate in fills 3-4, owner_unrouted (reported, not acted on) in 5-6,
  deferred pair_wait in 7-8. The liveness property is per UNIT on the slice.
  This is the fixture for the slice-liveness plan in technical_debt.md.

USAGE
  python3 scripts/starved_slice_units.py logs/events.jsonl RUN_ID [K]   (K default 3)
  (scripts/pattern_census.py reads `starved_by_fill` for its P45 row.)

HOW TO INTERPRET
  One line per fill: red tests, tests with an executed slice, (test, unit)
  pairs past K, then the starved units. "failure moved" = the test's first-
  failure stub (phase_c.crash.stub_holds) changed, or phase_c.tdd.failure_site_moved
  names it. Slice = phase_c.session.plan slices. Negative control: run
  3040-51eab48c (the 10-02 green resume) prints no starved unit at K=3.
"""
import json
import sys
from collections import defaultdict
from typing import Any

_EVENTS = ("phase_c.fill.schedule", "phase_c.session.plan",
           "phase_c.crash.stub_holds", "phase_c.tdd.failure_site_moved",
           "phase_c.tdd.iter")


def load_events(path: str, runs: set[str]) -> dict[str, dict[str, list[tuple[str, dict[str, Any]]]]]:
    """run -> event name -> [(ts, data)] for the events this measurement reads."""
    out: dict[str, dict[str, list[tuple[str, dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(list))
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if runs and not any(r in line for r in runs):
                continue
            for name in _EVENTS:
                if f'"{name}"' in line:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        break
                    r = str(e.get("run_id", ""))
                    if not runs or r in runs:
                        out[r][name].append((e["ts"], e.get("data") or e))
                    break
    return out


def _unit(label: str) -> str:
    return label.split(" (")[0].strip()


def starved_by_fill(ev: dict[str, list[tuple[str, dict[str, Any]]]], k: int = 3,
                    ) -> list[tuple[int, list[str], int]]:
    """Per fill: (fill, starved units, pairs past k) for one run's events."""
    def latest(name: str, ts: str) -> dict[str, Any] | None:
        xs = [d for t, d in ev.get(name, ()) if t <= ts]
        return xs[-1] if xs else None

    written_at: dict[str, int] = defaultdict(lambda: -1)
    moved_at: dict[str, int] = defaultdict(int)
    last_stub: dict[str, str | None] = {}
    streak: dict[tuple[str, str], int] = defaultdict(int)
    out: list[tuple[int, list[str], int]] = []
    for ts, d in ev.get("phase_c.fill.schedule", ()):
        n = int(d.get("iter") or 0)
        it = latest("phase_c.tdd.iter", ts)
        red = set(it.get("failed") or []) if it else set()
        holds = latest("phase_c.crash.stub_holds", ts)
        stub_of = {t: s for s, tt in ((holds or {}).get("held") or {}).items() for t in tt}
        for t in red:
            if last_stub.get(t) != stub_of.get(t):
                moved_at[t] = n
                last_stub[t] = stub_of.get(t)
        for t2, dm in ev.get("phase_c.tdd.failure_site_moved", ()):
            if t2 <= ts and dm.get("iter") == n:
                for t in dm.get("moved") or []:
                    moved_at[t] = n
        plan = latest("phase_c.session.plan", ts)
        slices = {t: {_unit(u) for u in us}
                  for t, us in ((plan or {}).get("slices") or {}).items()}
        opened: set[str] = set()
        for x in d.get("runs") or []:
            opened |= set(x.get("targets") or [])
        past: list[tuple[str, str]] = []
        for t in sorted(red):
            for u in slices.get(t) or ():
                key = (t, u)
                if written_at[u] >= moved_at[t] or u in opened:
                    streak[key] = 0
                    continue
                streak[key] += 1
                if streak[key] >= k:
                    past.append(key)
        for u in opened:
            written_at[u] = n
        out.append((n, sorted({u for _t, u in past}), len(past)))
    return out


def main(argv: list[str]) -> None:
    path, run = argv[1], argv[2]
    k = int(argv[3]) if len(argv) > 3 else 3
    ev = load_events(path, {run}).get(run, {})
    worst: dict[str, int] = defaultdict(int)
    run_len: dict[str, int] = defaultdict(int)
    for n, starved, pairs in starved_by_fill(ev, k):
        print(f"fill {n}: (test, unit) pairs past {k} fills {pairs}")
        print("     starved units:", starved)
        for u in list(run_len):
            if u not in starved:
                run_len[u] = 0
        for u in starved:
            run_len[u] += 1
            worst[u] = max(worst[u], run_len[u])
    print("fills starved in a row, per unit:", dict(sorted(worst.items())))


if __name__ == "__main__":
    main(sys.argv)
