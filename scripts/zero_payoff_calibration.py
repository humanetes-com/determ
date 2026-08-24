#!/usr/bin/env python3
"""Calibrate the zero-payoff window K from recorded firings and outcomes.

WHY THIS EXISTS
  The zero-payoff detector (domain/zero_payoff.py) has run in SHADOW since
  2026-08-16 (first event: run 1408670-3c074ece, 12:10:31 UTC) and its own
  wire-up rule demands that the window K governing its one action — on the
  Kth consecutive zero-payoff sweep, hand the escalation's measured producer
  set to the fill route — be chosen by comparing recorded firings against
  recorded outcomes, never by argument. Until 2026-08-24 that comparison had
  never been taken ("its firing lines have never been compared against
  outcomes"). The event store now holds 18 `phase_c.tdd.zero_payoff_shadow`
  events across runs 1408670-3c074ece → 40938-f4e8e94b, so the comparison is
  a pure offline read. This script IS that pre-registered comparison.

USAGE
  python scripts/zero_payoff_calibration.py [path/to/events.jsonl ...]
  No arguments: reads logs/events.jsonl. No GPU, no vLLM, no pipeline state
  touched; safe while a run is live. Re-run whenever new shadow firings
  accumulate — the conclusion is only as current as the event store.

WHAT IT COMPUTES
  Per (run, cluster), from `phase_c.tdd.iter` events: the judged-sweep
  sequence, each sweep identified by (pass_count, sorted failed_pairs;
  falling back to failed[] on old rows — pair vectors, because bare counts
  cannot express a test that runs in two binaries). The sweep at iter N
  judges the batch dispatched at iter N-1, so a transition counts toward a
  flat streak ONLY when the prior iteration dispatched > 0 units — the
  empty-frontier spin (run 1246060-2339497b, thousands of cached sweeps
  with nothing dispatched) satisfies no part of the detector's condition
  and is excluded. Traversals come from `pass_completed`: the pass ends at
  the dispatch marked pass_completed=True and is judged by the next sweep;
  a pass is FLAT when every sweep judging one of its batches moved nothing.
  `phase_c.tdd.final_sweep` rows carry no pair vector (count-only) and are
  shown but excluded from streaks.

HOW TO INTERPRET THE OUTPUT
  Two units are reported, because the record refutes one of them:
  - SWEEP units: for every verdict move, L = consecutive flat dispatched
    transitions immediately before it. A firing at K <= max(L) intervenes
    on an iteration ordinary dispatch was about to redeem. The known
    redemption at L=3 (run 3124988-ac6886ba, +9 at iter 5) is MID-PASS
    flatness: iters 2-4 judged batches 1-3 of a 4-batch traversal of the
    43-unit escalated cohort, and the +9 is the pass-complete reading. Any
    sweep-count K <= B-1 (B = batches per traversal) reproduces that miss,
    and B grows with the cohort, so NO fixed sweep-count generalises —
    the same finding the stop-knob arithmetic already establishes for
    TDD_FLAT_SCORE_REPEATS vs _REQUIRE_TRAVERSAL.
  - TRAVERSAL units: "completed flat passes later redeemed within the same
    run" is the decisive table. A redemption whose move follows a COHORT
    CHANGE (frontier_fp/cohort_fp moved, e.g. an escalation widening) is
    flagged: it is evidence FOR acting at the flat pass (what paid was a
    change to what is dispatched, which is D's action), not against it.
  DECISION RULE (pre-registered shape, unit corrected): D fires on the
  first COMPLETED flat traversal of an unchanged cohort — K = 1 in
  traversal units — unless this script finds a completed flat pass whose
  cohort was unchanged and whose later sweeps moved anyway; then K = that
  count + 1. The would-fire lines preview the live cost of the choice.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path


def load(paths):
    sweeps = defaultdict(list)
    fires = defaultdict(list)
    finals = {}
    for p in paths:
        with open(p) as f:
            for line in f:
                if '"phase_c.tdd.' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = e.get("event", "")
                d = e.get("data", {}) or {}
                key = (e.get("run_id", "?"), d.get("cluster", "?"))
                if ev == "phase_c.tdd.iter":
                    pairs = d.get("failed_pairs")
                    vec = tuple(sorted(map(str, pairs))) if pairs is not None \
                        else tuple(sorted(map(str, d.get("failed") or ())))
                    sweeps[key].append({
                        "iter": d.get("iter"), "ts": e.get("ts", ""),
                        "pass": d.get("pass_count"), "fail": d.get("fail_count"),
                        "ident": (d.get("pass_count"), vec),
                        "dispatched": d.get("dispatched") or 0,
                        "pass_completed": bool(d.get("pass_completed")),
                        # identity of the set being traversed: fingerprint when
                        # the row has one (added with 197's fact vocabulary),
                        # else the frontier size — enough to see an escalation
                        # widening on pre-fingerprint rows (3124988: 12 -> 39)
                        "cohort_id": d.get("frontier_fp") or ("size", d.get("frontier_size")),
                    })
                elif ev == "phase_c.tdd.zero_payoff_shadow":
                    fires[key].append((d.get("iter"), len(d.get("targets") or ())))
                elif ev == "phase_c.tdd.final_sweep":
                    finals[key] = (d.get("pass_count"), d.get("fail_count"))
    return sweeps, fires, finals


def main():
    paths = [Path(a) for a in sys.argv[1:]] or [Path("logs/events.jsonl")]
    sweeps, fires, finals = load(paths)

    sweep_Ls = []          # L before each move, dispatched transitions only
    flat_pass_redeemed = []  # (run, pass_iters, later_move_iter, cohort_changed)
    n_flat_passes = 0
    total_fires = sum(len(v) for v in fires.values())

    for key in sorted(sweeps, key=lambda k: sweeps[k][0]["ts"]):
        run, cluster = key
        seq = sweeps[key]
        has_fire = key in fires
        # moves & streaks over dispatched transitions
        moves, flat = [], 0
        for i in range(1, len(seq)):
            if seq[i - 1]["dispatched"] <= 0:
                continue
            if seq[i]["ident"] == seq[i - 1]["ident"]:
                flat += 1
            else:
                pv, cv = seq[i - 1]["pass"], seq[i]["pass"]
                dp = (cv - pv) if cv is not None and pv is not None else 0
                # the batch this move judged is the one dispatched at i-1
                moves.append((seq[i]["iter"], flat, dp, seq[i - 1]["cohort_id"]))
                sweep_Ls.append(flat)
                flat = 0
        # traversals: segments ending at a pass_completed dispatch,
        # judged flat iff no move landed at any sweep judging its batches
        passes = []
        start = 0
        for i, s in enumerate(seq):
            if s["pass_completed"] and s["dispatched"] > 0:
                judged = i + 1 < len(seq)
                if judged:
                    move_iters = {m[0] for m in moves}
                    span = [seq[j]["iter"] for j in range(start + 1, i + 2)
                            if j < len(seq)]
                    is_flat = judged and not (set(span) & move_iters)
                    passes.append((seq[start]["iter"], s["iter"], is_flat))
                    if is_flat:
                        n_flat_passes += 1
                        for m in moves:
                            if m[0] > s["iter"] + 1:
                                # cohort changed iff the moved batch traversed a
                                # different set than the flat pass did
                                changed = m[3] != s["cohort_id"]
                                flat_pass_redeemed.append(
                                    (run, (seq[start]["iter"], s["iter"]),
                                     m[0], changed))
                start = i + 1
        if not (has_fire or moves or passes):
            continue
        desc = " ".join(f"{s['iter']}:{s['pass']}/{s['fail']}" for s in seq[:30])
        if len(seq) > 30:
            desc += f" …({len(seq)} sweeps)"
        print(f"{run} [{cluster}] {desc}"
              + (f" final:{finals[key][0]}/{finals[key][1]}(count-only)"
                 if key in finals else ""))
        for fit, ntg in fires.get(key, ()):
            print(f"    FIRED iter={fit} targets={ntg}")
        for mit, L, dp, _cid in moves:
            print(f"    MOVE  iter={mit} L={L} dpass={dp:+d}")
        for a, b, is_flat in passes:
            print(f"    PASS  iters {a}..{b} {'FLAT' if is_flat else 'moved'}")
        for r, (a, b), mit, ch in flat_pass_redeemed:
            if r == run:
                print(f"    ** flat pass {a}..{b} later redeemed at iter {mit}"
                      f" — cohort {'CHANGED between' if ch else 'UNCHANGED'} **")

    print()
    print(f"shadow firings recorded: {total_fires}")
    print(f"sweep-unit: moves={len(sweep_Ls)}, max L before a move = "
          f"{max(sweep_Ls) if sweep_Ls else 0} "
          f"(sweep-count K would need > this and does not generalise; see header)")
    unchanged = [r for r in flat_pass_redeemed if not r[3]]
    print(f"traversal-unit: completed flat passes = {n_flat_passes}; "
          f"later redeemed = {len(flat_pass_redeemed)} "
          f"(cohort unchanged: {len(unchanged)})")
    print("K (traversal units, rule in header) = "
          + ("1 — fire on the first completed flat traversal of an unchanged cohort"
             if not unchanged else
             "RE-DERIVE: an unchanged-cohort redemption exists (rows above)"))


if __name__ == "__main__":
    main()
