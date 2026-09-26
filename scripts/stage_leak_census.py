#!/usr/bin/env python3
"""Where do dispatched units and GPU minutes go between the loop's stages?

WHY THIS EXISTS
  Run `38934-2999cc94` (2026-09-26) sat two fills on a recursion whose two
  units were in every dispatch batch and reached no route. Whether that is one
  defect or a class was a question the run's own log could not answer; the
  route ledger and the event log across 465 runs can. Three readings, each the
  evidence for one point of the plan that owns them:
    LEAK   dispatch -> route: of the units each iteration dispatched, how many
           the landed-change audit says no route attempted, and where each one
           went (an executed run's targets, only deferred runs' targets, or no
           run's targets at all — a filter with no recorded reason).
    YIELD  which route kinds bought verdicts, per GPU minute, stratified by
           whether the tree still had stubs (`hollow`) — the mix of routes the
           budget went to against the mix that paid.
    DEFER  why scheduled runs did not run.

USAGE
  python scripts/stage_leak_census.py [--since YYYY-MM-DD] [--run RUN_ID ...]
  Reads logs/events.jsonl (`phase_c.tdd.landed_change`, `phase_c.fill.schedule`)
  and logs/route_ledger/anyhow/index.jsonl. Read-only; no GPU, no model. The
  event log is not rotated by run, so `--since` bounds it; the landed-change
  audit carries `unattempted` only from 2026-08-31.

HOW TO INTERPRET
  LEAK  `unattempted / dispatched` is the share of dispatched units that no
        route attempted. It is NOT all defect: a unit whose only run was
        deferred `premise_moved` or `landed_this_fill` exited legitimately.
        The three buckets separate the cases:
          attempted-elsewhere  the ledger lists the unit in an executed run's
                               targets while the audit calls it unattempted —
                               the two accountings disagree; the cause is
                               unverified (a session/pair run may not credit
                               its write set as an attempt). Fix the
                               accounting before reading the other buckets.
          deferred-only        every run holding the unit was deferred; the
                               reason column says which rule kept it out.
          no-run               NO scheduled run held the unit: dropped before
                               planning with no recorded reason. This is the
                               bucket a filter between dispatch and the route
                               planners lives in; a unit here on two
                               consecutive iterations is the stall shape.
        `chronic` counts units unattempted on >=2 consecutive iterations of
        one run; look at the run column before reading the unit column.
  YIELD `tests/hr` is verdict gain per GPU hour (max(net_pass_delta, 0) summed
        over runs / minutes); `runs that gained` is the share of runs whose net
        was positive. Read the two strata separately — a stub fill's gain is
        not evidence about an endgame route. A route kind with many minutes and
        few tests is where the budget is not following payoff; small strata
        (n < 30 runs) are direction, not rate. Selection bias applies: a route
        only ran where the planner chose it.
  DEFER reasons are the schedule's own; `pair_wait` is a whole-fill deferral
        of the runs held behind a pair, counted per waiter.
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "events.jsonl"
LEDGER = ROOT / "logs" / "route_ledger" / "anyhow" / "index.jsonl"


def _rows(since: str, runs: set[str]) -> list[dict]:
    out = []
    with LEDGER.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if runs and r["run_id"] not in runs:
                continue
            if since and str(r.get("started", ""))[:10] < since:
                continue
            out.append(r)
    return out


def _events(name: str, since: str, runs: set[str]) -> list[dict]:
    out = []
    needle = f'"{name}"'
    with EVENTS.open() as fh:
        for line in fh:
            if needle not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") != name:
                continue
            if runs and e.get("run_id") not in runs:
                continue
            if since and e["ts"][:10] < since:
                continue
            out.append(e)
    return out


def leak(since: str, runs: set[str], rows: list[dict]) -> None:
    fills: dict[tuple[str, int], list[dict]] = collections.defaultdict(list)
    for r in rows:
        fills[(r["run_id"], r["iteration"])].append(r)
    by_run: dict[str, list[tuple[int, dict]]] = collections.defaultdict(list)
    for e in _events("phase_c.tdd.landed_change", since, runs):
        if "unattempted" in e["data"]:
            by_run[e["run_id"]].append((e["data"]["iter"], e["data"]))
    iters = with_leak = disp = unatt = 0
    chronic: dict[tuple[str, str], int] = {}
    buckets: collections.Counter = collections.Counter()
    reasons: collections.Counter = collections.Counter()
    no_run: collections.Counter = collections.Counter()
    no_run_runs: set[str] = set()
    for run, lst in by_run.items():
        lst.sort(key=lambda x: x[0])
        streak: collections.Counter = collections.Counter()
        for it, d in lst:
            iters += 1
            disp += d["dispatched"]
            u = set(d["unattempted"])
            unatt += len(u)
            with_leak += bool(u)
            for k in list(streak):
                if k not in u:
                    del streak[k]
            for k in u:
                streak[k] += 1
                if streak[k] >= 2:
                    chronic[(run, k)] = max(chronic.get((run, k), 0), streak[k])
            rs = fills.get((run, it))
            if rs is None:
                continue
            executed: set[str] = set()
            deferred: dict[str, set[str]] = collections.defaultdict(set)
            for r in rs:
                for f, n in r["targets"]:
                    unit = f"{f}::{n}"
                    if r["outcome"] == "deferred":
                        deferred[unit].add(r["reason"])
                    else:
                        executed.add(unit)
            for unit in u:
                if unit in executed:
                    buckets["attempted-elsewhere"] += 1
                elif unit in deferred:
                    buckets["deferred-only"] += 1
                    for why in deferred[unit]:
                        reasons[why] += 1
                else:
                    buckets["no-run"] += 1
                    no_run[unit] += 1
                    no_run_runs.add(run)
    if not iters:
        print("LEAK: no landed-change events with `unattempted` in this window")
        return
    print(f"LEAK  runs {len(by_run)}  iterations {iters}  "
          f"with >=1 unattempted {with_leak} ({100 * with_leak / iters:.0f}%)")
    print(f"      dispatched units {disp}  unattempted {unatt} "
          f"({100 * unatt / max(disp, 1):.1f}%)")
    matched = sum(buckets.values())
    for name in ("attempted-elsewhere", "deferred-only", "no-run"):
        print(f"      {name:20s} {buckets[name]:4d} "
              f"({100 * buckets[name] / max(matched, 1):.0f}% of {matched} "
              "unit-iterations joined to a same-fill ledger)")
    print(f"      deferred-only reasons: {dict(reasons.most_common())}")
    print(f"      no-run: {sum(no_run.values())} unit-iterations in "
          f"{len(no_run_runs)} run(s); most repeated: "
          f"{no_run.most_common(5)}")
    runs_c = {r for r, _ in chronic}
    print(f"      chronic (>=2 consecutive iterations): {len(chronic)} unit(s) "
          f"in {len(runs_c)} run(s)")


def yields(since: str, runs: set[str], rows: list[dict]) -> None:
    hollow: dict[tuple[str, int], int] = {}
    for e in _events("phase_c.fill.schedule", since, runs):
        hollow[(e["run_id"], e["data"]["iter"])] = e["data"].get("hollow") or 0
    agg: dict[tuple[str, str], list[float]] = collections.defaultdict(
        lambda: [0.0, 0, 0, 0])
    for r in rows:
        if r["outcome"] == "deferred":
            continue
        h = hollow.get((r["run_id"], r["iteration"]))
        if h is None:
            continue
        a = agg[("hollow>0" if h else "endgame", r["kind"])]
        gain = max(r.get("net_pass_delta") or 0, 0)
        a[0] += r.get("minutes") or 0
        a[1] += gain
        a[2] += 1
        a[3] += 1 if gain else 0
    print("YIELD (executed runs only; stratum = hollow>0 vs endgame)")
    for stratum in ("hollow>0", "endgame"):
        tot_min = sum(v[0] for (s, _k), v in agg.items() if s == stratum)
        tot_gain = sum(v[1] for (s, _k), v in agg.items() if s == stratum)
        for (s, kind), (m, g, n, c) in sorted(agg.items()):
            if s != stratum:
                continue
            print(f"      {s:9s} {kind:8s} runs {n:4d}  min {m:7.0f} "
                  f"({100 * m / max(tot_min, 1):3.0f}% of minutes)  gained {int(g):4d} "
                  f"({100 * g / max(tot_gain, 1):3.0f}% of gain)  "
                  f"tests/hr {60 * g / m if m else 0:5.2f}  "
                  f"runs that gained {100 * c / n:3.0f}%")


def defers(rows: list[dict]) -> None:
    out = collections.Counter(r["outcome"] for r in rows)
    why = collections.Counter(r["reason"] for r in rows
                              if r["outcome"] == "deferred")
    n = sum(out.values())
    print(f"DEFER scheduled rows {n}: {dict(out.most_common())}")
    print(f"      deferred reasons: {dict(why.most_common())}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    ap.add_argument("--run", action="append", default=[])
    args = ap.parse_args()
    runs = set(args.run)
    rows = _rows(args.since, runs)
    leak(args.since, runs, rows)
    yields(args.since, runs, rows)
    defers(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
