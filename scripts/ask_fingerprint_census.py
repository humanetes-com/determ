#!/usr/bin/env python3
"""WHY. The loop admits another try at a red test only when the test's own
failure text moved (`candidate_tiers.ask_fingerprint`, read by `due_reading`
through `AskLedger.spent`), and it ends a cluster as failed only when every
stub and tier of every red test is spent under its CURRENT fingerprint (the
`cluster_failed` break in `run_phase_c_tdd`). A wrong body that prints a
different wrong value is a new fingerprint, so a test whose wrong bodies keep
changing what they print is re-asked without bound, and the cluster never
reaches "nothing new can be asked". Over `logs/events.jsonl` from 2026-10-09
to 2026-11-03 the `exhausted` kind of `phase_c.tdd.cluster_failed` was never
emitted (two `crashed`, zero `exhausted`). The review of the loop-supervisor
plan on 2026-11-03 named one decision the code does not take: a bound on the
number of CHANGED-fingerprint asks per red test. Whether such a bound is safe
depends on one measured fact: after how many changed fingerprints has a red
test ever still gone green. This script measures it.

USAGE.  python scripts/ask_fingerprint_census.py [--events logs/events.jsonl]
        [--lane run|oracle] [--min-fps 1] [--list N]
Read-only over the event stream, no GPU, safe while a pipeline runs.
`--lane run` judges each process on its own. `--lane oracle` (default) chains
consecutive processes whose sweeps report the same oracle size, which is how
the ask ledger persists across a resume (it is keyed by oracle). A
`restart_from.sh task2a` drops the ledger but often keeps the oracle size, so
the chained view can over-count fingerprints across such a restart; compare
both lanes before sizing a knob.

HOW TO INTERPRET THE OUTPUT. Per lane, one line: runs, sweeps with an owed
reading, red tests seen, tests that went green, tests red at the lane's end.
Then the tallies over every lane:
  * fps_before_green histogram: for each test that went green, how many
    DISTINCT fingerprints it was owed under (outside a held stub) before the
    sweep it passed on. 1 means its first readable failure was the one the
    landing body fixed; k means k-1 changed-input re-asks preceded the green.
  * fps_while_red histogram: for each test still red at its lane's end, the
    distinct fingerprints it was owed under. The tail of this histogram is
    the unbounded re-ask the bound would stop.
  * max_fps_before_green: the largest k above. THE DECISION RULE: a
    per-test cap on changed-fingerprint asks must sit ABOVE this number, or
    it withdraws a test the record shows went green. would_withdraw(K) is how
    many still-red tests a cap of K would have withdrawn, and
    would_have_killed(K) how many tests that went green it would have
    withdrawn first (must be 0).
  * owed sweeps under the fingerprint it went green on / under the last
    fingerprint, still red: how many owed readings a test sat under ONE
    unchanged failure text. A long still-red tail with a short green
    distribution means the unchanged-text re-asks (other tier units, other
    rungs, up to TDD_OWED_MISSES offers each) are where a lane spends its
    end-game, not changed-text re-asks.
  * owed tier on the reading before green / at the end, still red: which
    tier of the test's reach-ranked slice was being asked when it went green,
    and where the still-red tests sit. If green comes from tier 0 and the
    red tests still have many tiers ahead, the terminal "nothing new can be
    asked" is far away and the asks still owed are the outer tiers.
  * asks_to_terminal>=N per lane: the fewest landings the lane still owed
    at its last sweep before `DueReading.exhausted` could be true, assuming
    every ask lands and no failure text changes (each change re-opens the
    test's tiers, each miss costs TDD_OWED_MISSES offers). Divide by the
    lane's landings per sweep for the sweeps the loop's own close was away.
  * repeat share: of consecutive owed sweeps of one red test, how often the
    fingerprint was unchanged (the ledger already stops those) vs changed (a
    fresh ask).
`--list N` prints the N red-at-end tests with the most fingerprints, with
their lane, so the per-fill read can open those fills.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

OWED = '"phase_c.tdd.owed"'
ITER = '"phase_c.tdd.iter"'


@dataclass
class TestTrack:
    fps: list[str] = field(default_factory=list)   # distinct, in order
    last_fp: str = ""
    repeats: int = 0
    changes: int = 0
    green_at: int = -1          # sweep index in lane it first passed on
    fps_before_green: int = 0
    sweeps_on_fp: dict[str, int] = field(default_factory=dict)
    sweeps_to_green_on_last: int = 0
    last_tier: int = -2          # owed_tier at the last owed reading
    tier_at_green: int = -2
    max_tier: int = -1
    tiers_n: int = 0
    to_terminal: int = 0         # least asks left under the current text


@dataclass
class Lane:
    key: str
    runs: list[str] = field(default_factory=list)
    sweeps: int = 0
    tests: dict[str, TestTrack] = field(default_factory=dict)
    red_now: set[str] = field(default_factory=set)


def _asks_left(row: dict) -> int:
    """The fewest asks `due_reading` still owes this test before it is
    spent under its current fingerprint: each owed unit on its owed rung and
    the rungs after it, every unit of every later tier on every rung, one
    landing each (a miss costs `TDD_OWED_MISSES` offers, so this is a floor)."""
    tiers = row.get("tiers") or []
    ot = int(row.get("owed_tier", -1))
    rungs = row.get("rungs") or {}
    owed = row.get("owed") or {}
    n = sum(_RUNGS - _RUNG_AT.get(rungs.get(u, "unit"), 0) for u in owed)
    if ot >= 0:
        n += sum(_RUNGS * len(t) for t in tiers[ot + 1:])
    return n


_RUNG_AT = {"unit": 0, "session": 1}
_RUNGS = len(_RUNG_AT)


def _lane_key(mode: str, run: str, oracle: object) -> str:
    return run if mode == "run" else f"oracle={oracle}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl", type=Path)
    ap.add_argument("--lane", choices=("run", "oracle"), default="oracle")
    ap.add_argument("--min-fps", type=int, default=1)
    ap.add_argument("--list", type=int, default=0)
    args = ap.parse_args()

    lanes: list[Lane] = []
    cur: Lane | None = None
    # The owed event precedes the iter event of the same sweep only when the
    # owed reading is taken; pair them by (run, iter).
    pending_owed: dict[tuple[str, int], dict] = {}
    run_oracle: dict[str, object] = {}

    with args.events.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            is_owed = OWED in line
            is_iter = ITER in line
            if not (is_owed or is_iter):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            run = ev.get("run_id", "")
            d = ev.get("data") or {}
            it = d.get("iter")
            if is_owed:
                pending_owed[(run, it)] = d
                continue
            oracle = d.get("oracle_size")
            if oracle is None or "failed" not in d:
                continue
            run_oracle[run] = oracle
            key = _lane_key(args.lane, run, oracle)
            if cur is None or cur.key != key:
                cur = Lane(key)
                lanes.append(cur)
            if not cur.runs or cur.runs[-1] != run:
                cur.runs.append(run)
            owed = pending_owed.pop((run, it), None)
            idx = cur.sweeps
            cur.sweeps += 1
            passed = set(d.get("passed") or ())
            failed = set(d.get("failed") or ())
            for t in passed:
                tr = cur.tests.get(t)
                if tr is not None and tr.green_at < 0 and t in cur.red_now:
                    tr.green_at = idx
                    tr.fps_before_green = len(tr.fps)
                    tr.sweeps_to_green_on_last = tr.sweeps_on_fp.get(
                        tr.last_fp, 0)
                    tr.tier_at_green = tr.last_tier
                cur.red_now.discard(t)
            cur.red_now |= failed
            if owed is None:
                continue
            for t, row in (owed.get("tests") or {}).items():
                if row.get("held_by"):
                    continue  # a held stub's text is "not implemented"
                fp = row.get("fingerprint") or ""
                if not fp:
                    continue
                tr = cur.tests.setdefault(t, TestTrack())
                if tr.green_at >= 0:
                    # Went green and came back red: a new episode.
                    tr.green_at, tr.fps, tr.last_fp = -1, [], ""
                    tr.sweeps_on_fp = {}
                if tr.last_fp:
                    if fp == tr.last_fp:
                        tr.repeats += 1
                    else:
                        tr.changes += 1
                if fp not in tr.fps:
                    tr.fps.append(fp)
                tr.sweeps_on_fp[fp] = tr.sweeps_on_fp.get(fp, 0) + 1
                tr.last_tier = int(row.get("owed_tier", -1))
                tr.max_tier = max(tr.max_tier, tr.last_tier)
                tr.tiers_n = len(row.get("tiers") or ())
                tr.to_terminal = _asks_left(row)
                tr.last_fp = fp

    before_green: Counter[int] = Counter()
    green_sweeps: Counter[int] = Counter()
    red_sweeps: Counter[int] = Counter()
    green_tier: Counter[int] = Counter()
    red_tier: Counter[int] = Counter()
    red_tiers_left = 0
    lane_terminal: list[tuple[str, int, int]] = []
    while_red: Counter[int] = Counter()
    repeats = changes = 0
    red_rows: list[tuple[int, str, str]] = []
    print(f"lanes ({args.lane}):")
    for ln in lanes:
        if not ln.tests:
            continue
        g = [t for t, tr in ln.tests.items() if tr.green_at >= 0]
        r = [t for t, tr in ln.tests.items()
             if tr.green_at < 0 and t in ln.red_now]
        print(f"  {ln.key}: runs={len(ln.runs)} ({ln.runs[0]}..{ln.runs[-1]}) "
              f"sweeps={ln.sweeps} owed_tests={len(ln.tests)} "
              f"green={len(g)} red_at_end={len(r)}")
        for t in g:
            before_green[ln.tests[t].fps_before_green] += 1
            green_sweeps[ln.tests[t].sweeps_to_green_on_last] += 1
            green_tier[ln.tests[t].tier_at_green] += 1
        if r:
            lane_terminal.append((ln.key, len(r),
                                  sum(ln.tests[t].to_terminal for t in r)))
        for t in r:
            n = len(ln.tests[t].fps)
            red_sweeps[ln.tests[t].sweeps_on_fp.get(
                ln.tests[t].last_fp, 0)] += 1
            red_tier[ln.tests[t].last_tier] += 1
            red_tiers_left += max(0, ln.tests[t].tiers_n
                                  - max(ln.tests[t].last_tier, 0))
            while_red[n] += 1
            red_rows.append((n, ln.key, t))
        for tr in ln.tests.values():
            repeats += tr.repeats
            changes += tr.changes

    def _hist(c: Counter[int]) -> str:
        return " ".join(f"{k}:{c[k]}" for k in sorted(c))

    print(f"fps_before_green: {_hist(before_green)}")
    print(f"fps_while_red:    {_hist(while_red)}")
    print(f"owed sweeps under the fingerprint it went green on: "
          f"{_hist(green_sweeps)}")
    print(f"owed sweeps under the last fingerprint, still red: "
          f"{_hist(red_sweeps)}")
    print(f"owed tier on the reading before green (-1 = joint/lesson/"
          f"none owed): {_hist(green_tier)}")
    print(f"owed tier at the lane's end, still red: {_hist(red_tier)} "
          f"(tiers still ahead, summed: {red_tiers_left})")
    print("asks to the terminal at each lane's end (red tests, least "
          "landings before `cluster_failed` can fire):")
    for key, nr, left in lane_terminal:
        print(f"  {key}: red={nr} asks_to_terminal>={left}")
    mx = max(before_green) if before_green else 0
    print(f"max_fps_before_green={mx}")
    for k in sorted({2, 3, 4, 5, 6, 8, mx + 1}):
        if k < 1:
            continue
        wd = sum(v for n, v in while_red.items() if n > k)
        kill = sum(v for n, v in before_green.items() if n > k)
        print(f"  cap K={k}: would_withdraw={wd} would_have_killed={kill}")
    tot = repeats + changes
    if tot:
        print(f"consecutive owed readings: unchanged={repeats} "
              f"changed={changes} ({100 * changes / tot:.0f}% fresh asks)")
    if args.list:
        print(f"top {args.list} red-at-end tests by fingerprints:")
        for n, key, t in sorted(red_rows, reverse=True)[:args.list]:
            if n >= args.min_fps:
                print(f"  {n:3d}  {key}  {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
