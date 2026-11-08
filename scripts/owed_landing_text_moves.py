#!/usr/bin/env python3
"""After a body lands for a (function, red test) pair the test OWED, does the
test's failure text move before the next owed reading, or stay the same?

WHY. The ask ledger (`domain/candidate_tiers.py::AskLedger`) spends a
(function, test) pair under one failure-text fingerprint as soon as one body
lands for it, and `due_reading` then walks to the next tier. So on the owed
path a pair can land a second body only after the test's text moved. A plan
of 2026-11-07 escalates a pair to the maximum reasoning level after K landed
medium bodies counted across fingerprints. Whether that trigger can fire for
the pairs that stay red depends on how often a landing leaves the text
unchanged (the pair is then spent with one landing and never re-asked under
that text). This script reads that off the record, using only the owed pairs
(the unit was in the test's owed set at the reading before the run), not the
coarse every-kept-unit-times-every-target pairing.

USAGE.
    python scripts/owed_landing_text_moves.py [--events logs/events.jsonl]
        [--since YYYY-MM-DD] [--product NAME]
Reads `phase_c.tdd.owed` (per test: owed units, fingerprint) and
`phase_c.route.run` (kept, target_tests, still_red, newly_green), per
run_id, in timestamp order. Read-only; no GPU.

HOW TO INTERPRET.
    landings            owed (unit, test) landings seen
    next=green_or_gone  the test was absent from the next owed reading
                        (green, withdrawn, or no later reading in the run)
    next=same_text      the next reading had the SAME fingerprint: the pair
                        is spent with this landing and the walk moves on
    next=text_moved     the next reading had a new fingerprint
    red_end N=k (s/m)   pairs whose test was still red at the run's last owed
                        reading, with k owed landings in the run; s of them
                        had their last landing followed by same_text, m by
                        text_moved; still_owed_at_end: the unit was in the
                        test's owed set at that last reading (the run ended
                        with the next ask of this pair still owed)
    red-at-end tests by max_N   per (run, test) still red at the run's last
                        owed reading, the largest owed-landing count over
                        its pairs (0: no owed landing at all)
    per test L          owed landings of ANY unit for the test in the run:
                        green_after = tests absent from the reading after
                        their L-th; red_at_end = tests still red with L
    per test A          as L, counting every owed unit a run offered
                        (opened, else targeted) whether or not it landed:
                        the count `AskLedger.low_asks` keeps, so the table
                        `TDD_ESCALATE_AFTER` is read from
    K table             for a candidate TDD_ESCALATE_AFTER: the share
                        of greens that came at or before the K-th ask, the
                        greens that came after it (asks past K at the same
                        level that paid), and the red-at-end tests K reaches
    walk ever empty     red-at-end tests that at some reading owed nothing
                        and were not held: that test's walk was spent under
                        its fingerprint (when a walk-exhaustion trigger
                        would fire)
Decision rule: a per-pair escalation "after K landings across fingerprints"
reaches a red-at-end test only if its max_N >= K, so the max_N table is the
share of stuck tests such a trigger can reach. A per-test trigger is sized
on the per-test table: K_t where green_after above K_t is small against
red_at_end at or above K_t, read on a lane of the level being escalated
from, never on another level's record.
"""
from __future__ import annotations

import argparse
import ast
import json
from collections import Counter, defaultdict
from pathlib import Path


def _units(raw: object) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return set()
    out: set[str] = set()
    for k in raw or []:
        if isinstance(k, (list, tuple)) and len(k) >= 2:
            out.add(f"{k[0]}::{k[1]}")
        elif isinstance(k, str):
            out.add(k)
    return out


def _tests(raw: object) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return set()
    return {str(t) for t in raw or []}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--since", default="2026-10-09")
    ap.add_argument("--product", default=None)
    a = ap.parse_args()
    runs: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    with a.events.open() as fh:
        for line in fh:
            if '"phase_c.tdd.owed"' in line:
                kind = "owed"
            elif '"phase_c.route.run"' in line:
                kind = "run"
            else:
                continue
            e = json.loads(line)
            if e.get("ts", "") < a.since or (a.product and e.get("product") != a.product):
                continue
            runs[e.get("run_id") or ""].append((e["ts"], kind, e.get("data") or {}))
    nxt: Counter[str] = Counter()
    red_end: Counter[int] = Counter()
    red_end_last: dict[int, Counter[str]] = defaultdict(Counter)
    test_max: Counter[int] = Counter()
    still_owed: Counter[int] = Counter()
    t_green_after: Counter[int] = Counter()
    t_red_end: Counter[int] = Counter()
    a_green_after: Counter[int] = Counter()
    a_red_end: Counter[int] = Counter()
    walk_end = Counter()
    n_runs = 0
    for _rid, seq in runs.items():
        seq.sort(key=lambda x: x[0])
        if not any(k == "owed" for _, k, _ in seq):
            continue
        n_runs += 1
        cur: dict[str, tuple[str, frozenset[str]]] = {}
        pending: list[tuple[str, str, str]] = []
        count: Counter[tuple[str, str]] = Counter()
        last: dict[tuple[str, str], str] = {}
        t_count: Counter[str] = Counter()
        a_count: Counter[str] = Counter()
        a_pending: Counter[str] = Counter()
        ever_empty: set[str] = set()
        for _ts, kind, d in seq:
            if kind == "run":
                kept = _units(d.get("kept"))
                offered = _units(d.get("opened")) or _units(d.get("targets"))
                for t, (_fp, owed) in cur.items():
                    a_pending[t] += len((kept | offered) & owed)
                if not kept:
                    continue
                tests = (_tests(d.get("target_tests")) | _tests(d.get("still_red"))
                         | _tests(d.get("newly_green")))
                for t in tests:
                    if t not in cur:
                        continue
                    fp, owed = cur[t]
                    for u in kept & owed:
                        pending.append((u, t, fp))
                continue
            tests_now = d.get("tests") or {}
            new = {t: (r.get("fingerprint", ""), frozenset(r.get("owed") or {}))
                   for t, r in tests_now.items()}
            for t, n in a_pending.items():
                a_count[t] += n
                if t not in new:
                    a_green_after[a_count.pop(t)] += 1
            a_pending = Counter()
            for t in {t for _u, t, _fp in pending}:
                t_count[t] += sum(1 for _u, tt, _fp in pending if tt == t)
                if t not in new:
                    t_green_after[t_count.pop(t)] += 1
            for u, t, fp in pending:
                if t not in new:
                    out = "green_or_gone"
                    count.pop((u, t), None)
                    last.pop((u, t), None)
                else:
                    out = "same_text" if new[t][0] == fp else "text_moved"
                    count[(u, t)] += 1
                    last[(u, t)] = out
                nxt[out] += 1
            pending = []
            cur = new
            for t, r in tests_now.items():
                if not (r.get("owed") or {}) and not r.get("held_by"):
                    ever_empty.add(t)
        per_test: dict[str, int] = {}
        for (u, t), n in count.items():
            if t in cur:
                red_end[n] += 1
                red_end_last[n][last[(u, t)]] += 1
                if u in cur[t][1]:
                    still_owed[n] += 1
                per_test[t] = max(per_test.get(t, 0), n)
        for t in cur:
            test_max[per_test.get(t, 0)] += 1
            t_red_end[t_count.get(t, 0)] += 1
            a_red_end[a_count.get(t, 0)] += 1
            walk_end["red_end_walk_ever_empty" if t in ever_empty
                     else "red_end_walk_never_empty"] += 1
            if not (cur[t][1]):
                walk_end["red_end_walk_empty_at_last_reading"] += 1
    print(f"runs_with_owed={n_runs} landings={sum(nxt.values())}")
    for k in ("green_or_gone", "same_text", "text_moved"):
        print(f"  next={k:14s} {nxt[k]}")
    print("red-at-end owed pairs by owed landings:")
    for n in sorted(red_end):
        c = red_end_last[n]
        print(f"  N={n:<3d} pairs={red_end[n]:<6d} last_same_text={c['same_text']:<6d} "
              f"last_text_moved={c['text_moved']} still_owed_at_end={still_owed[n]}")
    print("red-at-end tests by the most owed landings any one of their pairs had:")
    for n in sorted(test_max):
        print(f"  max_N={n:<3d} tests={test_max[n]}")
    print("per test, owed landings (any unit) before green / at a red end:")
    for n in sorted(set(t_green_after) | set(t_red_end)):
        print(f"  L={n:<3d} green_after={t_green_after[n]:<6d} red_at_end={t_red_end[n]}")
    print("per test, owed ASKS (landings + planned misses, any unit) before "
          "green / at a red end:")
    for n in sorted(set(a_green_after) | set(a_red_end)):
        print(f"  A={n:<3d} green_after={a_green_after[n]:<6d} red_at_end={a_red_end[n]}")
    g_all = sum(a_green_after.values())
    print("escalation count K (per-test asks): share of greens at or before K, "
          "greens after K, red-at-end tests at or above K")
    for k in (2, 3, 4, 6, 8, 10, 12, 15):
        at = sum(v for n, v in a_green_after.items() if n <= k)
        after = sum(v for n, v in a_green_after.items() if n > k)
        red = sum(v for n, v in a_red_end.items() if n >= k)
        print(f"  K={k:<3d} cum_green={at / g_all if g_all else 0:6.1%} "
              f"green_after={after:<6d} red_at_or_above={red}")
    print("red-at-end tests whose own owed walk was ever empty (nothing owed, not held):")
    for k in sorted(walk_end):
        print(f"  {k} {walk_end[k]}")


if __name__ == "__main__":
    main()
