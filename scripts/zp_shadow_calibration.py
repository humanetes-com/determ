#!/usr/bin/env python3
"""WHY. The consolidation program's step 2 makes authority conditional on
calibration: the shadow zero-payoff detector
(`domain/zero_payoff.py`, rebuilt 2026-08-16 onto the per-binary verdict axis)
may only graduate from logging to acting once its firings have been compared
against outcomes on live runs. That comparison had no instrument: the
detector's first live firing — run `1408670-3c074ece`, 2026-08-16T12:10:31Z,
cluster `stable`, iter 4, rule `unpaid_streak`, 30 targets — sat unexamined in
`logs/events.jsonl` while the flat-score guard stopped the same run one sweep
later (12:31:20Z), i.e. the shadow led the authoritative stop by one sweep with
a correct verdict, and nobody could cite that without re-deriving it by hand.
This script IS the tally: run it after any run in which the shadow fired (or
did not) and append the row it prints to that calibration record in
technical_debt.md.

USAGE.  python scripts/zp_shadow_calibration.py [--events logs/events.jsonl]
        [--horizon 3]
No GPU, no vLLM, read-only over the event stream; safe while a pipeline runs.
`--horizon` is how many subsequent scored sweeps a firing is judged against
(default 3, the stall budget's own denominator).

HOW TO INTERPRET THE OUTPUT. One row per shadow firing, then one per
authoritative stop, then the tally:
  * VINDICATED  — no scored sweep within the horizon rose above the firing
    sweep's pass_count. The detector called a real zero-payoff regime.
  * FALSE ALARM — a later sweep inside the horizon rose. Acting on this firing
    would have stopped (or re-aimed) a loop that was about to pay.
  * UNRESOLVED  — the run ended before the horizon filled AND no authoritative
    stop confirmed it; counts toward neither side.
  * lead=N on a vindicated row — the authoritative stop (flat-score guard /
    no-verdict clock) arrived N sweeps after the firing; the shadow's value is
    exactly this lead, bought without authority.
  * MISSED on a stop row — no shadow firing preceded that authoritative stop
    within the horizon: a false negative of the detector, the population the
    unpaid-streak rule was added for.
THE DECISION RULE: authority is arguable when, over at least
5 firings spanning at least 3 distinct runs, FALSE ALARM = 0 and
MISSED is not growing; a single false alarm resets the case — the cost of a
wrong stop is a killed climb, which is the P1 class, and no lead time pays
for one. Until those counts exist, the detector stays in shadow.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

SHADOW = "phase_c.tdd.zero_payoff_shadow"
ITER = "phase_c.tdd.iter"
STOPS = ("phase_c.tdd.flat_score_stop", "phase_c.tdd.no_verdict_stop")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl", type=Path)
    ap.add_argument("--horizon", default=3, type=int)
    args = ap.parse_args()

    # (run_id, cluster) -> ordered rows of interest, file order == time order.
    lanes: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with args.events.open() as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = row.get("event")
            if ev != SHADOW and ev != ITER and ev not in STOPS:
                continue
            data = row.get("data") or {}
            run = row.get("run_id") or ""
            if not run:
                continue      # pre-run_id rows predate the detector entirely
            lanes[(run, data.get("cluster") or "")].append(
                {"ev": ev, "ts": row.get("ts", ""), **data})

    firings = 0
    vindicated = 0
    false_alarms = 0
    unresolved = 0
    missed = 0
    fired_runs: set[str] = set()

    for (run, cluster), rows in sorted(lanes.items()):
        shadow_idx = [i for i, r in enumerate(rows) if r["ev"] == SHADOW]
        for i in shadow_idx:
            firings += 1
            fired_runs.add(run)
            base = next((r.get("pass_count") for r in reversed(rows[:i])
                         if r["ev"] == ITER and "pass_count" in r), None)
            after = [r for r in rows[i + 1:]
                     if r["ev"] == ITER and "pass_count" in r][:args.horizon]
            # Lead in SWEEPS = iteration delta, not intervening ITER events:
            # the stop is decided before its own iter event is emitted, so an
            # event count under-reads the lead by one on every eager stop.
            stop_row = next((r for r in rows[i + 1:] if r["ev"] in STOPS),
                            None)
            stop_lead = None
            if stop_row is not None:
                _a, _b = stop_row.get("iter"), rows[i].get("iter")
                stop_lead = (_a - _b if isinstance(_a, int)
                             and isinstance(_b, int) else 0)
            rose = base is not None and any(
                r["pass_count"] > base for r in after)
            if rose:
                false_alarms += 1
                verdict = "FALSE ALARM"
            elif after or stop_lead is not None:
                vindicated += 1
                verdict = ("VINDICATED"
                           + (f" lead={stop_lead}" if stop_lead is not None
                              else " (no authoritative stop followed)"))
            else:
                unresolved += 1
                verdict = "UNRESOLVED (run ended, horizon empty)"
            print(f"firing  {rows[i]['ts']}  {run}/{cluster}  "
                  f"iter={rows[i].get('iter')}  rule={rows[i].get('rule')}  "
                  f"targets={rows[i].get('target_count')}  base={base}  "
                  f"-> {verdict}")
        for j, r in enumerate(rows):
            if r["ev"] not in STOPS:
                continue
            preceding = sum(1 for k in shadow_idx
                            if k < j and sum(1 for x in rows[k:j]
                                             if x["ev"] == ITER)
                            <= args.horizon)
            if not preceding:
                missed += 1
            print(f"stop    {r['ts']}  {run}/{cluster}  {r['ev'].split('.')[-1]}"
                  f"  iter={r.get('iter')}  pass={r.get('pass_count')}"
                  + ("  MISSED (no shadow firing within the horizon)"
                     if not preceding else "  led by shadow"))

    print(f"\ntally: firings={firings} over {len(fired_runs)} run(s)  "
          f"vindicated={vindicated}  false_alarms={false_alarms}  "
          f"unresolved={unresolved}  stops_missed_by_shadow={missed}")
    print("authority case: needs >=5 firings over >=3 runs "
          "with false_alarms=0; a false alarm resets the case.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
