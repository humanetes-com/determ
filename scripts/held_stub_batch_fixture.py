#!/usr/bin/env python3
"""Recompute one sweep's dispatch batch from the run's events, under the
pre-fix owed/crash share and under the shipped rank (`domain/dispatch_rank`).

WHY THIS EXISTS
  On run `857717-812ee44c` (stable, iter 5) two still-stubbed functions held
  red tests at their first failure: `src/wrapper.rs::source` (3 tests) and
  `src/error.rs::object_drop_front` (1). Both were owed and both were
  crash-graded, and neither was in the 12-unit batch. The log line said they
  were "absent from this sweep's frontier"; that line was wrong. The cause was
  the owed/crash share the driver used then (`share_batch`, since deleted). A
  unit that was both crash-graded and owed was ranked inside the owed share by
  (tier, -cut_streak, name), so it lost its crash rank, and on the first
  sweep every key before the name tied. This script carries a frozen copy of
  that share as the PRE-FIX arm and checks it against what the schedule
  received (`inputs.due` on `phase_c.fill.schedule` = owed units in the
  batch): that reproduction is the negative control. The SHIPPED arm builds
  the same sweep's facts and calls `dispatch_rank.rank` and `take`; every
  held stub must be in its batch.

USAGE
  .venv/bin/python scripts/held_stub_batch_fixture.py [RUN_ID] [ITER] [CAP]
  Defaults: 857717-812ee44c 5 12. Reads logs/events.jsonl. Read-only.

  HELD is the first-failure stub set (`phase_c.crash.stub_holds`); the
  slice-based `hollow` count is printed beside it for comparison.

  The pre-fix arm assumes every owed unit has cut streak 0 for this process.
  That holds on the sweep where the held units first become owed, or after a
  sweep in which they were in the batch. If the reproduction check prints
  MISMATCH, the streaks were not all zero, or the sweep ran on code from
  before the owed share existed (run `39786-fd38e72f` and earlier).

HOW TO INTERPRET
  pre-fix share: first CAP        -- the batch the old code built
  reproduces inputs.due           -- OK means the recorded batch is that one
  held units, position pre-fix    -- > CAP means cut by the cap
  shipped rank: batch             -- held units must all be inside
  The final line prints ON-THEORY when the pre-fix share cuts at least one
  held unit and the shipped rank admits every held unit, NO CUT when the
  pre-fix share already admitted every held unit and the shipped rank does
  too, and OFF-THEORY otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.domain.dispatch_rank import (  # noqa: E402
    Crash,
    UnitFacts,
    rank,
    take,
)


def _pre_fix_share_batch(crash: list[str], owed: list[str], cap: int, *,
                         cut_streak: dict[str, int], tier_of: dict[str, int],
                         misses: int = 3) -> tuple[list[str], list[str]]:
    """Frozen copy of the deleted `candidate_tiers.share_batch`: the batch
    head the driver built when crash evidence and owed functions competed
    for `cap` slots. Kept only as this fixture's negative control."""
    owed_sorted = sorted(
        owed, key=lambda u: (tier_of.get(u, 0), -cut_streak.get(u, 0), str(u)))
    owed_set = set(owed_sorted)
    crash_only = [u for u in crash if u not in owed_set]
    if not owed_sorted:
        return list(crash), []
    if cap <= 0 or not crash_only or len(crash_only) + len(owed_sorted) <= cap:
        return [*crash_only, *owed_sorted], []
    starved = [u for u in owed_sorted if cut_streak.get(u, 0) >= misses > 0]
    rest_owed = [u for u in owed_sorted if u not in set(starved)]
    owed_share = min(len(owed_sorted), -(-cap // 2))
    crash_take = max(0, cap - owed_share - len(starved))
    return ([*starved, *crash_only[:crash_take], *rest_owed],
            list(crash_only[crash_take:]))


def _events(run_id: str, iter_n: int) -> tuple[dict, dict, dict]:
    owed_ev: dict = {}
    sched_ev: dict = {}
    hold_ev: dict = {}
    with (ROOT / "logs" / "events.jsonl").open() as fh:
        for line in fh:
            if run_id not in line:
                continue
            if ('"phase_c.tdd.owed"' in line or '"phase_c.fill.schedule"' in line
                    or '"phase_c.crash.stub_holds"' in line):
                d = json.loads(line)
                data = d.get("data") or {}
                if data.get("iter") != iter_n:
                    continue
                if d["event"] == "phase_c.tdd.owed":
                    owed_ev = data
                elif d["event"] == "phase_c.crash.stub_holds":
                    hold_ev = data
                else:
                    sched_ev = data
    return owed_ev, sched_ev, hold_ev


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else "857717-812ee44c"
    iter_n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    cap = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    owed_ev, sched_ev, hold_ev = _events(run_id, iter_n)
    if not owed_ev or not sched_ev:
        print(f"no owed/schedule event for {run_id} iter {iter_n}")
        return 2
    owed = list(owed_ev["owed"])
    inputs = sched_ev["inputs"]
    crash = list(inputs.get("crash_blamed", []))
    # HELD = the stub a red test's first failure sits in, observed on the
    # sweep's output (`phase_c.crash.stub_holds`). The schedule's slice-based
    # `hollow` map is printed for comparison only: it names every stub on a
    # test's executed slice, including ones run during unwinding after the
    # first panic, and on this run it is several times wider.
    held: dict[str, list[str]] = {u: list(ts) for u, ts in
                                  (hold_ev.get("held") or {}).items()}
    hollow: set[str] = {u for us in (inputs.get("hollow") or {}).values()
                        for u in us}
    tier_of: dict[str, int] = {}
    for row in owed_ev["tests"].values():
        for u in row.get("owed", {}):
            tier_of[u] = min(tier_of.get(u, 1 << 30), max(row.get("owed_tier", 0), 0))
    recorded_due = set(inputs.get("due", []))

    first, after = _pre_fix_share_batch(crash, sorted(owed), cap,
                                        cut_streak={}, tier_of=tier_of)
    batch = (first + after)[:cap]
    print(f"run {run_id} iter {iter_n} cap {cap}: {len(owed)} owed, "
          f"{len(crash)} crash-blamed, {len(held)} held stub(s) by first "
          f"failure, {len(hollow)} stub(s) on a red test's slice (hollow)")
    print("pre-fix share: first", cap, "=", batch)
    repro = set(batch) & set(owed) == recorded_due
    print("reproduces inputs.due:", "OK" if repro else
          f"MISMATCH (recorded {sorted(recorded_due)})")
    order = first + after
    for u in sorted(held):
        pos = order.index(u) + 1 if u in order else None
        print(f"  held {u} ({len(held[u])} test(s)): pre-fix position {pos} "
              f"-> {'in batch' if pos and pos <= cap else 'CUT'}")

    # The shipped rank on the same sweep's facts. The static order is not
    # recorded, so the owed set's name order stands in for it; it decides
    # only ties the facts leave, never a held unit's place.
    units = sorted(set(owed) | set(held) | set(crash))
    facts = {u: UnitFacts(
        static_index=i, held_tests=len(held.get(u, ())),
        crash=Crash.BLAMED if u in crash else None,
        owed_tier=tier_of.get(u, 0) if u in owed else None)
        for i, u in enumerate(units)}
    ranked = rank(facts)
    shipped = take(ranked, set(held), cap, cohort=None, traversed=())
    print("shipped rank: batch =", list(shipped.keys))
    cut_now = [u for u in held if u not in batch]
    admitted = all(u in shipped.keys for u in held)
    if not cut_now and admitted:
        print("NO CUT (the pre-fix share already admitted every held stub)")
    else:
        print("ON-THEORY" if cut_now and admitted else
              f"OFF-THEORY (cut pre-fix: {cut_now}, shipped admits all: "
              f"{admitted})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
