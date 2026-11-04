#!/usr/bin/env python3
"""Shadow of a yield-ranked fill plan, read off the route ledger; never executed.

WHY
---
The fill schedule opens and defers runs by static precedence: a pair before
the runs that share its tests (`pair_wait`), at most `SLICE_SESSION_MAX_PER_FILL`
sessions (`kind_cap`), a coverage split's extras behind its attributed write.
The audit of 2026-09-26 (78 runs, 1933 rows) found `pair_wait` deferred 486
waiters for a route kind that cashed 3% of its runs, and `kind_cap` 114
session deferrals of the kind that gained 71% of the endgame's verdicts.
The proposed fix (one `RouteYield` per route kind and tree stage that rank,
shares, caps and holds read) can only be judged on a shadow first: would a
yield-ranked plan have opened the runs the static plan deferred, AND did
those deferred runs' units later cash? This script is that reading. It
changes nothing.

USAGE
-----
    python scripts/route_yield_shadow.py                 # every run on record
    python scripts/route_yield_shadow.py --since 2026-10-01
    python scripts/route_yield_shadow.py --min-rows 5    # exploration floor

Reads `logs/route_ledger/anyhow/index.jsonl` (route rows) and the
`phase_c.tdd.iter` events of `logs/events.jsonl` (pass count and oracle per
sweep, for the stage). No cargo, no model, no GPU; under a minute.

The model it shadows, stated so it can be argued with:
  stage     build (pass/oracle < 0.5), climb (< 0.9), endgame (>= 0.9), read
            off the sweep that planned the fill.
  yield     tests turned green (`newly_green`) per opened minute, per (kind,
            stage), from rows STRICTLY BEFORE the fill (no look-ahead). A
            (kind, stage) with fewer than `--min-rows` opened rows has
            exploration priority (yield infinite), so no kind is starved of
            its next reading.
  shadow    opens a deferred run when its (kind, stage) yield is at least
            the lowest yield among the kinds the real plan opened in that
            fill. Re-buys are kept deferred by the shadow too (it shadows
            the precedence rules, not the re-buy rule).

HOW TO INTERPRET THE OUTPUT
---------------------------
  yield table     kind, stage, opened rows, minutes, tests gained, gained/h.
                  The ledger the shadow would rank on, at the end of the record.
  per reason      for each static deferral reason (pair_wait, kind_cap,
                  coverage_split, ...): deferred rows, how many the shadow
                  would have opened, and of those how many had a target that
                  a LATER row of the same run cashed (`later_cashed`) and the
                  median fills it waited.
  sessions        the entry's decision line: deferred sessions the shadow
                  would open whose units later cashed.
Decision rule (entry 293 B): build `RouteYield` only if the shadow would have
opened deferred runs whose units later cashed, in a material share of the
deferrals (the sessions line first), AND the opened runs it would have
displaced bought little in that fill. A shadow that would open runs whose
units never cashed is the static plan being right.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SELF_DEFERRALS = {"rebuy_flat", "rebuy_reverted", "rebuy_readmitted",
                   "budget_spent", "asked_this_fill", "landed_this_fill",
                   "premise_moved", "deferred", ""}


def _stage(ratio: float | None) -> str:
    if ratio is None:
        return "?"
    return "build" if ratio < 0.5 else "climb" if ratio < 0.9 else "endgame"


def _targets(row: dict) -> frozenset[tuple[str, str]]:
    return frozenset((str(a), str(b)) for a, b in row.get("targets") or ())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="", help="ISO date; rows started before are skipped")
    ap.add_argument("--min-rows", type=int, default=5, help="exploration floor per (kind, stage)")
    args = ap.parse_args()

    ratio: dict[tuple[str, int], float] = {}
    with open(ROOT / "logs" / "events.jsonl", errors="replace") as fh:
        for line in fh:
            if '"phase_c.tdd.iter"' not in line:
                continue
            ev = json.loads(line)
            d = ev.get("data") or {}
            oracle = d.get("oracle_size") or 0
            passed = d.get("passed")
            if not oracle or passed is None or "iter" not in d:
                continue
            ratio[(str(ev.get("run_id")), int(d["iter"]))] = len(passed) / oracle

    rows = [json.loads(line) for line in
            open(ROOT / "logs" / "route_ledger" / "anyhow" / "index.jsonl", errors="replace")
            if line.strip()]
    rows = [r for r in rows if str(r.get("started") or "") >= args.since]
    rows.sort(key=lambda r: str(r.get("started") or ""))
    fills: dict[tuple[str, int], list[dict]] = collections.defaultdict(list)
    for r in rows:
        fills[(str(r.get("run_id")), int(r.get("iteration") or 0))].append(r)

    seen_opened: dict[tuple[str, str], list[tuple[float, int]]] = collections.defaultdict(list)

    def yield_of(kind: str, stage: str) -> float:
        hist = seen_opened.get((kind, stage), [])
        if len(hist) < args.min_rows:
            return float("inf")
        minutes = sum(m for m, _ in hist) or 1e-9
        return sum(g for _, g in hist) / minutes

    per_reason: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    waits: dict[str, list[int]] = collections.defaultdict(list)
    sessions = collections.Counter()
    order = sorted(fills, key=lambda k: min(str(r.get("started") or "") for r in fills[k]))
    for key in order:
        run_id, it = key
        stage = _stage(ratio.get((run_id, it)))
        fill = fills[key]
        opened = [r for r in fill if r.get("outcome") not in ("deferred", "empty", "unopenable")]
        deferred = [r for r in fill if r.get("outcome") == "deferred"]
        floor = min((yield_of(str(r["kind"]), stage) for r in opened), default=float("inf"))
        later = [r for k2, rs in fills.items() if k2[0] == run_id and k2[1] > it for r in rs
                 if r.get("outcome") == "cashed"]
        for d in deferred:
            reason = str(d.get("reason") or "")
            if reason in _SELF_DEFERRALS:
                continue
            c = per_reason[reason]
            c["deferred"] += 1
            if yield_of(str(d["kind"]), stage) < floor:
                continue
            c["shadow_opens"] += 1
            tgt = _targets(d)
            hits = [r for r in later if _targets(r) & tgt]
            if hits:
                c["later_cashed"] += 1
                waits[reason].append(min(int(r.get("iteration") or 0) for r in hits) - it)
            if d.get("kind") == "session":
                sessions["shadow_opens"] += 1
                sessions["later_cashed"] += bool(hits)
        for r in opened:
            seen_opened[(str(r["kind"]), stage)].append(
                (float(r.get("minutes") or 0.0), len(r.get("newly_green") or ())))

    print("yield table (end of record): kind stage rows minutes gained gained/h")
    for (kind, stage), hist in sorted(seen_opened.items()):
        m = sum(x for x, _ in hist)
        g = sum(y for _, y in hist)
        print(f"  {kind:8} {stage:8} {len(hist):5} {m:8.0f} {g:6} {60 * g / m if m else 0:7.2f}")
    print("per static deferral reason: deferred shadow_opens later_cashed median_wait_fills")
    for reason, c in sorted(per_reason.items(), key=lambda kv: -kv[1]["deferred"]):
        w = waits.get(reason) or []
        print(f"  {reason:16} {c['deferred']:6} {c['shadow_opens']:6} {c['later_cashed']:6} "
              f"{statistics.median(w) if w else '-'}")
    print(f"sessions: shadow would open {sessions['shadow_opens']} deferred session(s); "
          f"{sessions['later_cashed']} of them had a target a later row cashed")


if __name__ == "__main__":
    main()
