#!/usr/bin/env python3
"""Distil one fill's recorded schedule DECISION into a fixture.

WHY. `tools/distill_fill.py` rebuilds a fill's candidates so a test can
re-rank them; it cannot reproduce that day's ranking (the body ledger's
per-unit verdict and the carved pieces are not in the event), so a replay of
the whole `schedule()` answers a different question from the one the fill
asked. The admission stage (`fill_schedule.admit_ranked`) takes the ranking
as given. This script keeps what that stage reads: the `phase_c.fill.schedule`
event verbatim (every candidate with its recorded rank, reason, level, hold,
and the recorded `inputs`), and the route ledger rows the stage's grace
clocks read. Run `82925-203ed7b8` fill 5 (2026-09-28) is the first fixture:
the recorded decision held the per-fn run behind two runs it then deferred.

USAGE
    python scripts/distill_schedule_decision.py --run 82925-203ed7b8 --iter 5 \
        [--cluster stable] [--project anyhow] \
        -o tests/fixtures/fills/82925-203ed7b8_iter5_decided.json
  Read-only over logs/events.jsonl and logs/route_ledger/<project>/index.jsonl;
  no GPU, no model. The event must carry `inputs` (fills from 2026-09-23 on).

WHAT THE LEDGER SLICE IS. The schedule loaded the first N cluster rows of the
index, N being the event's `ledger_rows`. Of those, the fixture keeps the rows
the admission stage can read: rows aimed at any candidate's tests (the
per-test grace clocks) and PAIR rows (a pair's own grace), under the fill's
oracle (every clock skips the others), with the fields those clocks read.
`oracle_fp` is the oracle of the newest such row of the same run, else of
the newest such row.

HOW TO READ THE OUTPUT. The script prints the fixture path, the candidate
count (opened / deferred), how many ledger rows were kept of the N loaded,
and the oracle. A fixture whose kept rows are 0 while the event records holds
is suspicious: the grace clocks then read an empty history.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    event = None
    with open(args.events, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.fill.schedule"' not in line or args.run not in line:
                continue
            ev = json.loads(line)
            d = ev.get("data") or {}
            if (ev.get("run_id") == args.run and d.get("iter") == args.iter
                    and d.get("cluster") == args.cluster):
                event = d
    if event is None or "inputs" not in event:
        raise SystemExit("no schedule event with inputs for that fill")

    n = int(event.get("ledger_rows") or 0)
    loaded: list[dict] = []
    index = Path("logs/route_ledger") / args.project / "index.jsonl"
    with open(index, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("cluster") == args.cluster:
                loaded.append(row)
                if len(loaded) >= n:
                    break
    tests: set[str] = set()
    for r in list(event.get("runs") or ()) + list(event.get("deferred") or ()):
        tests.update(r.get("target_tests") or ())
    relevant = [r for r in loaded
                if r.get("kind") == "pair"
                or tests & set(r.get("target_tests") or ())]
    own = [r for r in relevant if r.get("run_id") == args.run]
    oracle = (own or relevant or [{"oracle_fp": ""}])[-1].get("oracle_fp", "")
    # Every clock the stage reads skips rows under another oracle, and reads
    # only these fields of the rest.
    fields = ("kind", "label", "targets", "bodies_sha", "oracle_fp",
              "outcome", "run_id", "iteration", "cluster", "net_pass_delta",
              "target_tests", "newly_green", "reason")
    kept = [{k: r[k] for k in fields if k in r}
            for r in relevant if r.get("oracle_fp") == oracle]
    # A non-pair row is read only as "was it aimed at / did it turn green
    # this candidate test", so its lists keep the candidates' tests. A pair
    # row's aimed tests are part of its work key and stay whole.
    for r in kept:
        if r.get("kind") != "pair":
            r["target_tests"] = sorted(tests & set(r.get("target_tests") or ()))
            if isinstance(r.get("newly_green"), list):
                r["newly_green"] = sorted(
                    n for n in r["newly_green"]
                    if str(n).rsplit("::", 1)[-1] in tests)
    out = {"run_id": args.run, "iter": args.iter, "cluster": args.cluster,
           "oracle_fp": oracle, "ledger_loaded": n, "event": event,
           "ledger_rows": kept}
    Path(args.out).write_text(json.dumps(out, sort_keys=True) + "\n",
                              encoding="utf-8")
    print(f"wrote {args.out}: {len(event.get('runs') or ())} opened / "
          f"{len(event.get('deferred') or ())} deferred candidate(s); "
          f"{len(kept)} of {n} ledger row(s) kept; oracle {oracle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
