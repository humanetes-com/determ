#!/usr/bin/env python3
"""Call-price census: what a fill's run clock reserved per grant, per route kind.

WHY. `RunClock` reserves one call of a run's kind per granted claim
(`fill_schedule.call_price`). Before 2026-11-01 the price was taken only from
ledger rows on the CURRENT oracle, so the first fill after any oracle change
(every from-scratch run, every re-harvest) had no price, and a no-price grant
reserved the rest of the share: one call in flight on a four-wide pool. Run
`3178980-c42f7944` (2026-11-01) fill 1: the per-fn run ran 216.5 min, 55
calls, one unit at a time for all but a few minutes, and spent the fill's
whole 200-minute budget, so the 13 file and impl runs behind it were deferred
with 0 minutes. Minutes per call depend on the model and the route's prompt,
not on which tests fail, so the price is now read across oracles. This script
shows, for each run's first fill, the price the old rule gave (None = serial)
and the price the new rule gives, plus how stable minutes-per-call is across
oracles.

USAGE.
    python scripts/call_price_census.py [--project anyhow] [--cluster stable]
        [--kind per-fn] [--runs 8]
Reads `logs/route_ledger/<project>/index.jsonl` only. No GPU, no cargo.

HOW TO INTERPRET.
- One line per run (newest `--runs`), at its first fill (lowest iteration):
  `old=` the oracle-keyed price from rows written before that run started on
  the same oracle (None means the old clock was serial on that fill);
  `new=` the cross-oracle price from the same prior rows; `calls` and
  `min/call` are what that run's own rows of the kind then measured.
  A run whose `old=None` and `new=` close to its own `min/call` is a fill the
  old rule serialised for no reason the measurement supports.
- The last line, `spread`, is the median and the 10th–90th percentile of
  minutes per call over every row of the kind. A narrow spread across oracles
  is the premise of reading the price across oracles; a spread that widens by
  an order of magnitude would refute it.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_core.adapters.route_ledger_store import load_rows  # noqa: E402
from pipeline_core.domain.fill_schedule import (  # noqa: E402
    CALL_PRICE_WINDOW,
    RouteKind,
    call_price,
)


def _old_price(rows, kind, oracle_fp):
    graded = [r for r in rows if r.kind is kind and r.oracle_fp == oracle_fp
              and r.model_calls > 0 and r.minutes > 0]
    if not graded:
        return None
    return statistics.median(r.minutes / r.model_calls
                             for r in graded[-CALL_PRICE_WINDOW:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--kind", default="per-fn")
    ap.add_argument("--runs", type=int, default=8)
    a = ap.parse_args()
    kind = RouteKind(a.kind)
    rows = load_rows(Path(__file__).resolve().parents[1], a.project,
                     cluster=a.cluster)
    order: list[str] = []
    for r in rows:
        if r.run_id not in order:
            order.append(r.run_id)
    for run in order[-a.runs:]:
        first = min(r.iteration for r in rows if r.run_id == run)
        idx = next(i for i, r in enumerate(rows) if r.run_id == run)
        prior = rows[:idx]
        mine = [r for r in rows if r.run_id == run and r.iteration == first
                and r.kind is kind and r.model_calls > 0 and r.minutes > 0]
        fp = rows[idx].oracle_fp
        own = (f"calls={sum(r.model_calls for r in mine)} min/call="
               f"{statistics.median(r.minutes / r.model_calls for r in mine):.2f}"
               if mine else "calls=0")
        old = _old_price(prior, kind, fp)
        new = call_price(prior, kind)
        print(f"{run} fill {first} oracle {fp}: "
              f"old={'None' if old is None else f'{old:.2f}'} "
              f"new={'None' if new is None else f'{new:.2f}'} {own}")
    per = sorted(r.minutes / r.model_calls for r in rows
                 if r.kind is kind and r.model_calls > 0 and r.minutes > 0)
    if per:
        q = statistics.quantiles(per, n=10) if len(per) > 1 else [per[0]] * 9
        print(f"spread {kind.value}: n={len(per)} median={statistics.median(per):.2f} "
              f"p10={q[0]:.2f} p90={q[-1]:.2f} over "
              f"{len({r.oracle_fp for r in rows if r.kind is kind})} oracle(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
