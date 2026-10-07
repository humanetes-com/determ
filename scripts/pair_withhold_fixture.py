#!/usr/bin/env python
"""WHY — the measured-owners withholding a hop pair earns had no lapse, and
this computes, on the live records, whether giving it one changes anything.

Run `1158170-ce307ee7` (2026-09-15, cluster `stable`, killed at a plateau
after 208m10s) measured the executed slice of its three leftover tests at 29
distinct units, at both of its iterations, and dispatched 3. Its own log says
why, at both iterations: `measured-owners: 3 leftover test(s) already have a
hop-pair transaction — not admitting coverage owners those tests execute
through; the pair is the fill.` That skip is the first statement of
`escalate_to_measured_owners`'s per-test loop, so for a paired test no other
clause of the admission is ever evaluated. Its release condition is that the
leftover stops having a pair, and no fill can cause that: the attribution
re-derives the hop every iteration and a pair that REVERTS keeps it.

This script is the check that must pass before the word "validated" is
written about the lapse: it runs the SHIPPED predicates
(`domain/fill_schedule.unpaid_pair_fills`,
`domain/escalation_lifetime.withholding_paired_tests`) over the real route
ledger and the real coverage owner maps, and prints the negative controls
beside the claim. It computes nothing itself — a re-implementation here would
be a second reading that can drift from the one the loop uses, which is the
defect that made the pair's own diagnosis unreadable for a week.

USAGE

    python scripts/pair_withhold_fixture.py
    python scripts/pair_withhold_fixture.py --project anyhow --cluster stable \
        --run 1158170-ce307ee7 [--events logs/events.jsonl] [--no-events]

Reads only. No GPU, no vLLM, no workspace write. `--events` is a single pass
over `logs/events.jsonl` (267 MB on this box, a few seconds) to recover the
`phase_c.coverage.owner_map` records for `--run`; `--no-events` skips it and
prints the ledger half alone. The route ledger is
`logs/route_ledger/<project>/index.jsonl`, append-only, machine-local.

HOW TO INTERPRET THE OUTPUT

`unpaid` is how many graded PAIR runs aimed at that test have returned
nothing under the current oracle, newest first, stopping at the first that
cashed — the same walk the fill schedule already lapses a route's exemptions
on. `grace` is `DEFAULT_UNPAID_GRACE`, the shared bound.

* `WITHHOLDS` — `unpaid < grace`: the pair is still the paying fill and the
  coverage spine stays out of dispatch. This is the 675471 case the clause
  was written for and it must still print for a pair that has not been tried.
* `LAPSES` — `unpaid >= grace`: the pair's own record refutes the premise and
  the coverage owners that test executes through become admissible.

The `owner map` block is what the lapse buys, per iteration: `measured` is the
executed slice the run itself recorded, `dispatched` is what the batch held.
A lapse that does not widen `measured - dispatched` buys nothing and the
finding is elsewhere.

The three controls, all from the same rows, are printed under CONTROLS:

1. `prefix replay` — the streak recomputed as of each of the first `grace`
   graded pair rows in the record. Those must read WITHHOLDS: the lapse has
   to be EARNED by the pair's failures, not granted at the first buy.
2. `other oracle` — the same rows queried under a different oracle
   fingerprint. Must read 0 / WITHHOLDS: rows under another oracle are not
   evidence about this tree.
3. `pre-fix` — the withheld set with the lapse disabled (`unpaid_grace=0`),
   which is the code as it stood before. Must be every paired test, which is
   the bad outcome this reproduces.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.route_ledger_store import load_rows  # noqa: E402
from pipeline_core.domain.escalation_lifetime import (  # noqa: E402
    withholding_paired_tests,
)
from pipeline_core.domain.fill_schedule import (  # noqa: E402
    DEFAULT_UNPAID_GRACE,
    RouteKind,
    unpaid_pair_fills,
)


def _oracle_of(rows: list) -> str:
    """The oracle the newest row was written under — the tree's current one
    as far as the ledger can say. Passed in rather than guessed when the
    caller knows better."""
    for r in reversed(rows):
        if r.oracle_fp:
            return r.oracle_fp
    return ""


def _pair_tests(rows: list) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        if r.kind is RouteKind.PAIR:
            for t in r.target_tests:
                seen.setdefault(t, None)
    return sorted(seen)


def _owner_maps(events: Path, run: str) -> list[dict]:
    out: list[dict] = []
    try:
        fh = events.open(encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if run not in line or "phase_c.coverage.owner_map" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("event") == "phase_c.coverage.owner_map":
                out.append(d.get("data") or {})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--run", default="1158170-ce307ee7")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--no-events", action="store_true")
    ap.add_argument("--oracle", default="")
    args = ap.parse_args()

    rows = load_rows(ROOT, args.project, cluster=args.cluster)
    oracle = args.oracle or _oracle_of(rows)
    tests = _pair_tests(rows)
    grace = DEFAULT_UNPAID_GRACE
    print(f"ledger  {len(rows)} row(s) for {args.project}/{args.cluster}; "
          f"oracle {oracle}; grace {grace}")
    if not tests:
        print("no PAIR row in the ledger — nothing withholds, nothing lapses.")
        return 0

    unpaid = {t: unpaid_pair_fills(rows, t, oracle) for t in tests}
    withheld = withholding_paired_tests(tests, unpaid_fills=unpaid)
    print()
    for t in tests:
        verdict = "WITHHOLDS" if t in withheld else "LAPSES   "
        print(f"  {verdict}  unpaid {unpaid[t]:3d}/{grace}   {t}")
    print()
    print(f"=> {len(tests) - len(withheld)} of {len(tests)} paired leftover(s) "
          "admit their measured coverage owners")

    if not args.no_events:
        maps = _owner_maps(ROOT / args.events, args.run)
        print()
        for m in maps:
            union: set[str] = set()
            for ks in (m.get("owners") or {}).values():
                union |= {f"{k.get('file')}::{k.get('fn')}" for k in ks}
            print(f"  owner map  iter {m.get('iter')}: "
                  f"{len(m.get('failing') or ())} failing test(s), "
                  f"{len(union)} distinct measured owner(s)")
            print("             " + ", ".join(sorted(union)))
        if not maps:
            print(f"  owner map  no record in {args.events} for run {args.run}")

    print()
    print("CONTROLS")
    for t in tests[:1]:
        pair_rows = [r for r in rows
                     if r.kind is RouteKind.PAIR and t in r.target_tests
                     and r.oracle_fp == oracle]
        for n in range(1, grace + 1):
            prefix = pair_rows[:n]
            k = unpaid_pair_fills(prefix, t, oracle)
            w = withholding_paired_tests([t], unpaid_fills={t: k})
            print(f"  prefix replay  as of graded pair buy {n}: unpaid {k} -> "
                  + ("WITHHOLDS" if t in w else "LAPSES"))
    other = "0" * len(oracle) if oracle else "none"
    k_other = {t: unpaid_pair_fills(rows, t, other) for t in tests}
    w_other = withholding_paired_tests(tests, unpaid_fills=k_other)
    print(f"  other oracle   {other}: unpaid {sorted(set(k_other.values()))} -> "
          f"{len(w_other)} of {len(tests)} WITHHOLD")
    w_pre = withholding_paired_tests(tests, unpaid_fills=unpaid, unpaid_grace=0)
    print(f"  pre-fix        lapse disabled: {len(w_pre)} of {len(tests)} "
          "WITHHOLD (the lock this reproduces)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
