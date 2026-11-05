#!/usr/bin/env python3
"""Owner splits of owed red tests, over recorded sweeps: the pre-launch check.

WHY. On run 1604111-bb3716ef (2026-11-05, cluster stable, iter 8 and 9) the
loop held four views of which units an owed red test runs through. The rank
admitted six owners of two aborted tests, the owed walk's slice was the one
unit the session slicer had, and the admitted owners were removed as
"served, nothing owes". The run spent two fills (about 4 h) on units the
record already said could not move those tests. The same split appeared on
673664-014fd53a (2026-10-13) and nobody saw it, because nothing compared the
views. The loop now has one owner record (pipeline_core/domain/red_test.py)
and logs `HEALTH ‼ RED_TEST_SPLIT` when a sweep breaks it; this script reads
the same check off the event store, so a split shows up from a finished or
stopped run in seconds instead of after the next long run.

The checks (same rule as the loop, `red_test.owners_off_tiers`):
  I1 owners_off_tiers  an owner of an owed test on none of its tiers.
  I2 owed_off_frontier a unit the walk owes that a frontier filter removed.
Runs since the record shipped emit `phase_c.red_test.split` and are read
exactly (source=record). Older runs are reconstructed from the rank's owner
map (`phase_c.coverage.owner_map` -> owners) against `phase_c.tdd.owed`
tiers (source=reconstructed). I2 needs the new event; older runs print it
as n/a. The reconstruction cannot see a withheld hop pair (a paired test
takes attributed units only, by design), so it can overcount there.

USAGE.
  python scripts/red_test_invariants.py                 # last run in the window
  python scripts/red_test_invariants.py --run RUN_ID
  python scripts/red_test_invariants.py --all [--tail-bytes 0]
  Options: --events PATH (default logs/events.jsonl), --tail-bytes N (default
  80 MB from the end; 0 reads the whole file), --strict (exit 1 when a
  record-sourced sweep shows a split). No GPU, no cargo. pre_run.sh runs it
  without --strict as an advisory line.

HOW TO INTERPRET.
  One line per run: `RUN source=... sweeps=N split_sweeps=K tests=T`, then
  for each split sweep `iter I: TEST off=[units]` (I1) and `owed off: unit
  (reason)` (I2). split_sweeps=0 on a record-sourced run is the expected
  reading. On a reconstructed (old) run it is the historical census; a
  non-zero count there is the bug class the record closes, not a defect of
  the current code. A non-zero count on a record-sourced run means a reader
  still builds its own view of the owner set: find which reader (the unit
  names the session plan or the tiers lack) and route it through the record.
  The final `summary:` line totals runs, split sweeps, and record-sourced
  split sweeps; the last number is the one the decision rule reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.domain.red_test import owners_off_tiers  # noqa: E402

_KINDS = (b'"phase_c.red_test.split"', b'"phase_c.coverage.owner_map"',
          b'"phase_c.tdd.owed"')


def _kind(e: dict) -> str:
    for k in ("kind", "event", "name", "type"):
        v = e.get(k)
        if isinstance(v, str) and v.startswith("phase_c."):
            return v
    raw = json.dumps(e)[:400]
    for k in ("phase_c.red_test.split", "phase_c.coverage.owner_map",
              "phase_c.tdd.owed"):
        if k in raw:
            return k
    return ""


def _read(path: Path, tail: int, run: str | None):
    with path.open("rb") as fh:
        if tail > 0:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            if size > tail:
                fh.readline()
        for raw in fh:
            if not any(k in raw for k in _KINDS):
                continue
            if run and run.encode() not in raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--events", default=str(ROOT / "logs/events.jsonl"))
    ap.add_argument("--tail-bytes", type=int, default=80_000_000)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    path = Path(a.events)
    if not path.exists():
        print(f"red-test splits: no event store at {path}")
        return 0

    split: dict[tuple[str, int], dict] = {}
    owners: dict[tuple[str, int], dict] = {}
    tiers: dict[tuple[str, int], dict] = {}
    order: list[str] = []
    for e in _read(path, a.tail_bytes, a.run):
        d = e.get("data", e)
        run = e.get("run_id") or d.get("run_id") or "?"
        it = d.get("iter")
        if not isinstance(it, int):
            continue
        if run not in order:
            order.append(run)
        k = _kind(e)
        key = (run, it)
        if k == "phase_c.red_test.split":
            split[key] = d
        elif k == "phase_c.coverage.owner_map":
            owners[key] = {t: [f"{s['file']}::{s['fn']}" for s in ss]
                           for t, ss in (d.get("owners") or {}).items()}
        elif k == "phase_c.tdd.owed":
            tiers[key] = {t: v.get("tiers") or []
                          for t, v in (d.get("tests") or {}).items()}

    runs = order if (a.all or a.run) else order[-1:]
    if not runs:
        print("red-test splits: no owed reading in the window")
        return 0
    tot_split = tot_rec_split = 0
    for run in runs:
        keys = sorted({k for k in (*split, *tiers) if k[0] == run})
        rec = any(k in split for k in keys)
        lines: list[str] = []
        n_split = 0
        tests: set[str] = set()
        for key in keys:
            if rec:
                d = split.get(key)
                if d is None:
                    continue
                off = {t: set(v) for t, v in (d.get("owners_off_tiers") or {}).items()}
                owed_off = d.get("owed_off_frontier") or {}
            else:
                if key not in tiers:
                    continue
                off = {t: set(v) for t, v in owners_off_tiers(
                    owners.get(key, {}), tiers[key]).items()}
                owed_off = None
            if off or owed_off:
                n_split += 1
                tests |= set(off)
                for t, us in sorted(off.items()):
                    lines.append(f"  iter {key[1]}: {t} off=[{', '.join(sorted(us)[:6])}"
                                 + (f" +{len(us) - 6}" if len(us) > 6 else "") + "]")
                for u, r in sorted((owed_off or {}).items()):
                    lines.append(f"  iter {key[1]}: owed off: {u} ({r})")
        src = "record" if rec else "reconstructed"
        print(f"{run} source={src} sweeps={len(keys)} split_sweeps={n_split} "
              f"tests={len(tests)}" + ("" if rec else " I2=n/a"))
        for ln in lines[:40]:
            print(ln)
        tot_split += n_split
        tot_rec_split += n_split if rec else 0
    print(f"summary: runs={len(runs)} split_sweeps={tot_split} "
          f"record_split_sweeps={tot_rec_split}")
    return 1 if (a.strict and tot_rec_split) else 0


if __name__ == "__main__":
    sys.exit(main())
