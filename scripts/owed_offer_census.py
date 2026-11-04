#!/usr/bin/env python3
"""WHY. The loop's close ("nothing new can be asked", `DueReading.exhausted`,
the `cluster_failed` break in `run_phase_c_tdd`) needs every owed unit of
every red test to be spent: a body landed under the test's current failure
text, or `TDD_OWED_MISSES` offers landed nothing. `DueReading.answered`
charges an ask or a miss only for an owed unit the fill OFFERED (the batch
plus the units a run opened). An owed unit no run of the fill targets is
"neither", by that function's own docstring, so it moves the close by
nothing. Run `3203804-8c6a0066` (2026-11-03): iter 3 owed 15 units and the
fill answered 6; iter 4 owed 18 and answered 2. Over the event record the
`exhausted` close was never emitted. This script measures, per sweep, how
many owed units a run of that sweep's fill targeted at all, and for how many
consecutive sweeps an owed unit went untargeted.

USAGE.  python scripts/owed_offer_census.py [--events logs/events.jsonl]
        [--run RUN_ID] [--list N]
Read-only over the event stream, no GPU, safe while a pipeline runs. Reads
`phase_c.tdd.owed` (the owed units of sweep N), `phase_c.route.run` (each
run of fill N: targets, outcome) and `phase_c.tdd.asked` (what the loop
charged after fill N). A sweep whose fill never finished (the process ended
inside it) has no `asked` row and is reported as `cut`.

HOW TO INTERPRET THE OUTPUT. One line per sweep:
  owed=N    owed units the sweep's due reading named;
  ran=a     of those, targeted by a run of the fill that executed (any
            outcome other than DEFERRED / EMPTY / zero minutes);
  idle=b    targeted only by runs that did not execute (deferred, empty,
            unopenable session);
  none=c    targeted by no run of the fill at all;
  charged=d asks + misses the loop recorded after the fill (`asked` event);
            `cut` when the fill did not finish.
Then the totals over every sweep, and over finished fills only (a cut fill
inflates none= with the runs it never reached). Then the streak histogram: for each owed unit, the longest
run of consecutive sweeps it was owed and not executed (idle or none). THE
DECISION RULE: the close can only approach at the rate owed units are
executed. If none+idle is a large share of owed and streaks exceed the
dispatch rank's own bound (every EVIDENCED unit offered within
ceil(|EVIDENCED| / (cap - |held|)) sweeps), the owner is the dispatch and
the schedule, not the ask ledger: an owed unit is being kept out, which the
end goal forbids ("no hold can keep it out indefinitely").
`--list N` prints the N owed units with the longest untargeted streaks.
`--endgame K` (default 25) adds the transition table: for each red test,
sweep N to N+1 of one process, with at most K red tests at N+1, one of
held / left (green or withdrawn) / text moved (its ask re-armed at tier 0) /
same text, ask advanced / same text, nothing advanced. For the last class,
each owed unit of the test at N is split by what fill N did with it:
ran|idle|none as above, crossed with asked|missed|uncharged from the
`asked` event. `ran,uncharged` is a unit a run targeted that was in neither
the batch nor the units the engine reported opened (that is how
`DueReading.answered` decides), so it had no turn. The close moves only on
asked and missed; the uncharged rows are where it stands still.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

OWED = '"phase_c.tdd.owed"'
RUN = '"phase_c.route.run"'
ASKED = '"phase_c.tdd.asked"'
_IDLE = ("deferred", "empty", "unopenable")


def _executed(d: dict) -> bool:
    out = str(d.get("outcome") or "").lower()
    if any(k in out for k in _IDLE):
        return False
    return float(d.get("minutes") or 0.0) > 0.0 or int(
        d.get("model_calls") or 0) > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl", type=Path)
    ap.add_argument("--run", default="")
    ap.add_argument("--list", type=int, default=0)
    ap.add_argument("--endgame", type=int, default=25)
    args = ap.parse_args()

    owed: dict[tuple[str, int], set[str]] = {}
    ran: dict[tuple[str, int], set[str]] = defaultdict(set)
    idle: dict[tuple[str, int], set[str]] = defaultdict(set)
    charged: dict[tuple[str, int], int] = {}
    tests: dict[tuple[str, int], dict] = {}
    answered: dict[tuple[str, int], tuple[set[str], set[str]]] = {}
    order: list[tuple[str, int]] = []
    with args.events.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if args.run and args.run not in line:
                continue
            kind = (OWED if OWED in line else RUN if RUN in line
                    else ASKED if ASKED in line else "")
            if not kind:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            run, d = ev.get("run_id", ""), ev.get("data") or {}
            if kind == OWED:
                key = (run, int(d.get("iter", 0)))
                owed[key] = set(d.get("owed") or ())
                tests[key] = d.get("tests") or {}
                order.append(key)
            elif kind == RUN:
                key = (run, int(d.get("iteration", 0)))
                tg = {f"{f}::{n}" for f, n in d.get("targets") or ()}
                (ran if _executed(d) else idle)[key].update(tg)
            else:
                key = (run, int(d.get("iter", 0)))
                a, m = d.get("asked"), d.get("missed")
                answered[key] = (set(a) if isinstance(a, list) else set(),
                                 set(m) if isinstance(m, list) else set())
                charged[key] = (len(a) if isinstance(a, list) else int(a or 0)) + (
                    len(m) if isinstance(m, list) else int(m or 0))

    tot = Counter()
    streak: dict[tuple[str, str], int] = {}
    best: dict[tuple[str, str], int] = {}
    prev_run = ""
    for key in order:
        run, it = key
        if run != prev_run:
            streak = {k: v for k, v in streak.items() if k[0] == run}
            prev_run = run
        ow = owed[key]
        r = ow & ran.get(key, set())
        i = (ow & idle.get(key, set())) - r
        n = ow - r - i
        ch = charged.get(key)
        tot.update(owed=len(ow), ran=len(r), idle=len(i), none=len(n))
        if ch is not None:
            tot.update(charged=ch, f_owed=len(ow), f_ran=len(r),
                       f_idle=len(i), f_none=len(n), f_sweeps=1)
        print(f"{run} iter {it}: owed={len(ow)} ran={len(r)} idle={len(i)} "
              f"none={len(n)} charged={'cut' if ch is None else ch}")
        for u in ow:
            k = (run, u)
            streak[k] = 0 if u in r else streak.get(k, 0) + 1
            best[k] = max(best.get(k, 0), streak[k])
        for k in [k for k in streak if k[0] == run and k[1] not in ow]:
            streak.pop(k)
    print(f"total: owed={tot['owed']} ran={tot['ran']} idle={tot['idle']} "
          f"none={tot['none']} charged={tot['charged']}")
    print(f"finished fills only ({tot['f_sweeps']} sweeps): "
          f"owed={tot['f_owed']} ran={tot['f_ran']} idle={tot['f_idle']} "
          f"none={tot['f_none']} charged={tot['charged']}")
    h = Counter(best.values())
    print("longest owed-and-not-executed streak per (run, unit): "
          + " ".join(f"{k}:{h[k]}" for k in sorted(h)))
    trans: Counter[str] = Counter()
    still: Counter[str] = Counter()
    for (run, it), ta in tests.items():
        tb = tests.get((run, it + 1))
        if tb is None or len(tb) > args.endgame:
            continue
        aa, mm = answered.get((run, it), (set(), set()))
        k_ = (run, it)
        for t, ra in ta.items():
            rb = tb.get(t)
            if rb is None:
                trans["left (green/withdrawn)"] += 1
            elif ra.get("held_by") or rb.get("held_by"):
                trans["held"] += 1
            elif ra.get("fingerprint") != rb.get("fingerprint"):
                trans["text moved (re-armed)"] += 1
            elif ((ra.get("owed_tier"), ra.get("owed"), ra.get("rungs"))
                  != (rb.get("owed_tier"), rb.get("owed"), rb.get("rungs"))):
                trans["same text, ask advanced"] += 1
            else:
                trans["same text, nothing advanced"] += 1
                for u, rung in (ra.get("rungs") or {}).items():
                    st = ("ran" if u in ran.get(k_, set()) else
                          "idle" if u in idle.get(k_, set()) else "none")
                    ch = ("asked" if u in aa else "missed" if u in mm
                          else "uncharged")
                    still[f"{rung},{st},{ch}"] += 1
    print(f"end-game transitions (<= {args.endgame} red): "
          + ", ".join(f"{k}={v}" for k, v in trans.most_common()))
    print("  owed units of the stood-still tests: "
          + ", ".join(f"{k}={v}" for k, v in still.most_common()))
    if args.list:
        for (run, u), v in sorted(best.items(), key=lambda x: -x[1])[:args.list]:
            print(f"  {v:3d}  {run}  {u}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
