#!/usr/bin/env python3
"""Which recorded owners of an owed red test never reach its owed tiers or its session.

WHY. On run 1604111-bb3716ef (2026-11-05, cluster stable, iter 8 and 9) the
two owed red tests left were each given ONE owed unit, the Drop body of the
error type, while the rank and the measured-owners admission admitted six
owners for the same tests (three of them free fns of the vtable). Their
binary aborted, so `withhold_unmeasured` removed them from the coverage
owner map the session slicer reads; the rank kept what the aborted run did
enter (`_owners_positive`). The owed walk's slice is
`merge_slices(primary, attributed, ...)`, whose `primary` is the session
slicer's map; once it is non-empty, what the frontier attributes is never
read. The admitted owners then rank OTHER (not owed), and `asked_unchanged`
removes them from the frontier as "served, nothing owes".

A second, latent path to the same loss is `recorded_locator_sites`, which
converts a RECORDED owner set (used when a test has no positive owner) into
session sites and keeps a locator only when the attribution's site frontier
names it with a NON-EMPTY container: a free fn can never survive it. The
--tree block below replays that function; it did not fire on the run above
(its recorded owner set was empty).

This script computes, per (run, iter, owed red test):
  owners   = the owner set the rank read (positive coverage, else recorded)
             (`phase_c.attribution.measured_owners` -> owners)
  tiers    = every unit of the owed walk's tiers (`phase_c.tdd.owed` -> tests)
  dropped  = owners - tiers
  removed  = dropped units that `phase_c.dispatch.asked_unchanged` took off
             the frontier the same iteration
and, with --tree, replays `recorded_locator_sites` (the latent recorded-set
path) on the cluster tree's own extractor records (the most generous site
frontier possible: every site the tree declares) to show which owners would
survive the container rule at all if they had come from a recorded set.

USAGE.
  python scripts/owner_slice_census.py [--run RUN_ID] [--events logs/events.jsonl]
      [--tail-bytes N] [--tree tmp/anyhow/.skeleton-filled/stable]
  No GPU, no cargo. --tree shells the release extractor
  (tools/skeleton_extractor/target/release/skeleton-extractor --enumerate-fns).
  Without --run, every run in the read window is reported.

HOW TO INTERPRET.
  One line per (run, iter, test): `owners=N tiers=M dropped=K removed=R`, then
  the dropped units marked `free` (empty container on the tree) or `cont`.
  The decision rule: a row with dropped>0 AND removed>0 is a test whose
  recorded owners were admitted and then taken off the frontier while the
  walk owed something else; every such row is a fill that could not reach
  the bodies the test executes. The --tree block prints, for each test,
  `survive=` (what `recorded_locator_sites` keeps even when every tree site
  is in the frontier) and `lost_free=` (owners lost only because they are
  free fns). lost_free non-empty means no site frontier can ever carry them
  into a session; the fix has to change the rule, not the frontier.
  The summary line counts runs, rows with dropped>0, rows with removed>0.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _events(path: Path, tail: int, run: str | None):
    with path.open("rb") as fh:
        if tail > 0:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail))
            if size > tail:
                fh.readline()
        for raw in fh:
            if run and run.encode() not in raw:
                continue
            if (b'"phase_c.attribution.measured_owners"' not in raw
                    and b'"phase_c.tdd.owed"' not in raw
                    and b'"phase_c.dispatch.asked_unchanged"' not in raw):
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def _kind(e: dict) -> str:
    for k in ("kind", "event", "name", "type"):
        v = e.get(k)
        if isinstance(v, str) and v.startswith("phase_c."):
            return v
    raw = json.dumps(e)
    for k in ("phase_c.attribution.measured_owners", "phase_c.tdd.owed",
              "phase_c.dispatch.asked_unchanged"):
        if k in raw:
            return k
    return ""


def _tree_records(tree: Path) -> list:
    from pipeline_core.domain.models import FnRecord
    binary = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"
    out = subprocess.run([str(binary), "--enumerate-fns", "--input", str(tree)],
                         capture_output=True, text=True, check=True)
    return [FnRecord.from_dict(r) for r in json.loads(out.stdout)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--events", default=str(ROOT / "logs/events.jsonl"))
    ap.add_argument("--tail-bytes", type=int, default=400_000_000)
    ap.add_argument("--tree")
    a = ap.parse_args()

    owners: dict[tuple[str, int], dict[str, list[str]]] = {}
    tiers: dict[tuple[str, int], dict[str, set[str]]] = {}
    removed: dict[tuple[str, int], set[str]] = defaultdict(set)
    for e in _events(Path(a.events), a.tail_bytes, a.run):
        d = e.get("data", e)
        run = e.get("run_id") or d.get("run_id") or "?"
        it = d.get("iter")
        if not isinstance(it, int):
            continue
        k = _kind(e)
        if k == "phase_c.attribution.measured_owners":
            owners.setdefault((run, it), {}).update(d.get("owners") or {})
        elif k == "phase_c.tdd.owed":
            tiers[(run, it)] = {
                t: {u for tier in (v.get("tiers") or []) for u in tier}
                for t, v in (d.get("tests") or {}).items()}
        elif k == "phase_c.dispatch.asked_unchanged":
            removed[(run, it)].update(d.get("units") or [])

    free: set[str] = set()
    sites = []
    if a.tree:
        recs = _tree_records(Path(a.tree))
        from pipeline_core.domain.models import FnSite
        for r in recs:
            key = f"src/{r.file}" if not r.file.startswith("src/") else r.file
            if not r.container:
                free.add(f"{key}::{r.fn_name}")
            sites.append(FnSite(key, r.fn_name, r.container))

    runs, rows_drop, rows_rm = set(), 0, 0
    last: dict[str, tuple[str, int]] = {}
    for (run, it), per in sorted(owners.items()):
        tt = tiers.get((run, it), {})
        for t, us in sorted(per.items()):
            if t not in tt:
                continue
            runs.add(run)
            dropped = sorted(set(us) - tt[t])
            rm = sorted(set(dropped) & removed.get((run, it), set()))
            rows_drop += bool(dropped)
            rows_rm += bool(rm)
            marks = ", ".join(f"{u} ({'free' if u in free else 'cont'})"
                              if a.tree else u for u in dropped)
            print(f"{run} iter {it} {t}: owners={len(us)} tiers={len(tt[t])} "
                  f"dropped={len(dropped)} removed={len(rm)}"
                  + (f"\n    dropped: {marks}" if dropped else "")
                  + (f"\n    tiers:   {', '.join(sorted(tt[t]))}" if dropped else ""))
            last[t] = (run, it)

    if a.tree and last:
        from pipeline_core.domain.models import FnLocator
        from pipeline_core.domain.slice_session import recorded_locator_sites
        print("\n--tree replay of recorded_locator_sites, every tree site in the frontier:")
        for t, key in sorted(last.items()):
            us = owners[key][t]
            rec = {t: [FnLocator(*u.rsplit("::", 1)) for u in us]}
            got = recorded_locator_sites(rec, sites).get(t, frozenset())
            kept = sorted(f"{s.file}::{s.fn_name}" for s in got)
            lost_free = sorted(u for u in us if u in free)
            print(f"  {t}: owners={len(us)} survive={kept} lost_free={lost_free}")
    print(f"\nsummary: runs={len(runs)} rows_dropped={rows_drop} rows_removed={rows_rm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
