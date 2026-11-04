#!/usr/bin/env python3
"""How far route runs overrun their fill-budget share, and how much of the
overrun is fix-loop re-entry after a retrospective.

WHY THIS EXISTS.  Run `271137-e728ae95` (2026-09-23) opened its per-fn run on
a 29-minute share and closed it `FLAT after 66.6 min`.  Units in flight
complete by design (a share is checked between units, never inside one), but
a per-fn unit whose fix loop re-enters after a retrospective can run far past
the share it opened on: `src/error.rs::construct_from_boxed` and `context`
each re-entered "with the new guidance" while the screen retired that
guidance, and spent three more calls each on the same E0277.  Before sizing
anything (a per-unit bound, a smaller share, a cap on retro rounds) the
overrun has to be measured and split by cause.  Since 2026-09-23 a round
whose guidance would not reach the next prompt no longer re-enters
(`retro_gate.informed_reentry`); this script is how that change's effect on
the overrun is read.

USAGE.
  python scripts/share_overrun.py [--since YYYY-MM-DD] [--run RUN_ID ...]
    --since   route rows started at or after this date (default: 7 days
              before the newest row).
    --run     restrict to these run ids.
  Read-only: logs/route_ledger/<project>/index.jsonl and logs/events.jsonl
  (`phase_c.fn.fill.start`, `.done`, `.fail`, `phase_c.fn.retro_retry`,
  `phase_c.fn.retro_retry_withheld`).  No GPU, no model, no writes.

HOW TO INTERPRET THE OUTPUT.
  <kind>  n=N over=K  ratio p50/p90/max  excess=E min of share S min
      per route kind, the runs that opened on a positive share: how many ran
      past it, minutes/share quantiles, and the minutes spent past the share
      against the minutes granted.  A p90 well above 1 is a route whose
      shares do not bound it.
  per-fn units: n=U  re-entered=R  re-entry minutes=M of unit minutes T
      over per-fn runs that overran: the units whose fix loop re-entered
      after a retrospective, and the unit-minutes spent after the first
      re-entry (fill start to the first `retro_retry`, then to the unit's end).
      M/T is the share of the overrun's unit time that re-entry bought.
  withheld re-entries: W
      rounds the screen refused a re-entry for (post-2026-09-23 code).
  Decision rule: if M/T is large and re-entries are mostly withheld on the
  next reading, the overrun was the uninformed re-entry and nothing else
  needs sizing.  If it stays large with informed re-entries, the next lever
  is bounding re-entry rounds by the run's remaining share, sized on this
  distribution; if M/T is small, the overrun is first-round work and a unit
  bound is the question, not the retro loop.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--run", nargs="*", default=[])
    ap.add_argument("--project", default="anyhow")
    args = ap.parse_args(list(argv) if argv is not None else None)
    runs = set(args.run)
    with open(ROOT / "logs" / "route_ledger" / args.project / "index.jsonl",
              encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    rows = [r for r in rows if not runs or r.get("run_id") in runs]
    since = args.since
    if since is None and rows:
        newest = max(r.get("started", "") for r in rows)[:10]
        since = (datetime.fromisoformat(newest)
                 - timedelta(days=7)).date().isoformat()
    opened = [r for r in rows
              if (r.get("share_minutes") or 0) > 0
              and r.get("outcome") not in ("deferred", "empty", "unopenable")
              and r.get("started", "")[:10] >= (since or "")]
    print(f"route runs on a positive share since {since}: {len(opened)}")
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for r in opened:
        by_kind[r["kind"]].append(r)
    for kind, rs in sorted(by_kind.items()):
        ratios = sorted(r["minutes"] / r["share_minutes"] for r in rs)
        over = [r for r in rs if r["minutes"] > r["share_minutes"]]
        excess = sum(max(0.0, r["minutes"] - r["share_minutes"]) for r in rs)
        share = sum(r["share_minutes"] for r in rs)
        print(f"  {kind:8s} n={len(rs)} over={len(over)}  ratio "
              f"p50 {ratios[len(ratios) // 2]:.2f} / p90 "
              f"{ratios[min(len(ratios) - 1, int(len(ratios) * 0.9))]:.2f} / "
              f"max {ratios[-1]:.2f}  excess={excess:.0f} min of share "
              f"{share:.0f} min")

    # Per-fn units of the per-fn runs that overran.
    windows = [(r["run_id"], _ts(r["started"]),
                _ts(r["started"]) + 60.0 * r["minutes"])
               for r in by_kind.get("per-fn", [])
               if r["minutes"] > r["share_minutes"]]
    wanted = {w[0] for w in windows}
    # One record per unit FILL (a unit is filled again in a later fill of
    # the same run): [start, first retro_retry or None, end or None].
    fills: dict[tuple[str, str], list[list[float | None]]] = defaultdict(list)
    withheld = 0
    with open(ROOT / "logs" / "events.jsonl", encoding="utf-8",
              errors="replace") as fh:
        for line in fh:
            if '"phase_c.fn.' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("run_id") not in wanted:
                continue
            ev = e["event"]
            d = e.get("data") or {}
            key = (e["run_id"], f"{d.get('file')}::{d.get('fn')}")
            t = _ts(e["ts"])
            if ev == "phase_c.fn.fill.start":
                fills[key].append([t, None, None])
            elif not fills.get(key) or fills[key][-1][2] is not None:
                if ev == "phase_c.fn.retro_retry_withheld":
                    withheld += 1
                continue
            elif ev == "phase_c.fn.retro_retry":
                if fills[key][-1][1] is None:
                    fills[key][-1][1] = t
            elif ev in ("phase_c.fn.fill.done", "phase_c.fn.fill.fail"):
                fills[key][-1][2] = t
            elif ev == "phase_c.fn.retro_retry_withheld":
                withheld += 1
    unit_min = 0.0
    reentry_min = 0.0
    units = 0
    reentered = 0
    for key, recs in fills.items():
        for t0, retry, t1 in recs:
            if t0 is None or t1 is None or not any(
                    w[0] == key[0] and w[1] <= t0 <= w[2] for w in windows):
                continue
            units += 1
            unit_min += (t1 - t0) / 60.0
            if retry is not None:
                reentered += 1
                reentry_min += (t1 - retry) / 60.0
    print(f"per-fn units in overrun per-fn runs: n={units}  re-entered="
          f"{reentered}  re-entry minutes={reentry_min:.0f} of unit minutes "
          f"{unit_min:.0f}")
    print(f"withheld re-entries: {withheld}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
