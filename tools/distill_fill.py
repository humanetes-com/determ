#!/usr/bin/env python3
"""Distil one fill's schedule inputs from `logs/events.jsonl` into a fixture.

WHY. The walk harness (`tests/walks/harness.py`) replays the TDD loop with
the FILL stubbed whole, so no walk can assert anything about the order the
routes ran in or the budget they had. The fill schedule
(`domain/fill_schedule.py`) decides exactly that, and its recorded fixtures
have to come from the record the way the walks' do — from the events the
loop wrote, never from a hand-made list of units.

WHAT IT READS. For `--run RUN_ID --iter N`:
  - `phase_c.session.plan` of that fill: the leftover tests, each test's
    executed slice (site labels), the packed sessions, the hollow count;
  - `phase_c.fill.schedule` when the fill ran under the schedule (post-fix
    logs) — its runs are the candidates verbatim;
  - `phase_c.fill.route_plan` / `phase_c.file.fill.start` of that fill —
    which files the file route opened (pre-fix logs);
  - `phase_c.route.run` rows of EARLIER fills of the same cluster, as the
    ledger the schedule would have read.

WHAT IT CANNOT READ, and how the fixture says so. Pre-fix events carry no
stub inventory. `--stubs a::b,c::d` names the stubs the fixture assumes;
the fixture records them under `stubs` with `stubs_source: "argument"`, and
the recorded test checks that the hollow relation they induce on the
recorded slices reproduces the plan event's `hollow` count — the fixture is
wrong when it does not, and the test says so instead of passing. Post-fix
events carry the stubs on the schedule's runs and need no argument.

USAGE
    python tools/distill_fill.py --run 334902-2774708c --iter 11 \
        --stubs src/error.rs::object_downcast,src/error.rs::object_boxed \
        -o tests/fixtures/fills/334902-2774708c_iter11.json

HOW TO READ THE OUTPUT. The fixture is JSON: `run_id`, `iter`, `cluster`,
`tests` (name -> [sites]), `sessions` ([{tests, units}] as packed),
`hollow_recorded` (the plan event's count), `stubs`, `stubs_source`,
`file_runs` ([{file, targets, stubs}] — from the schedule event when present,
else one per file that holds a stub, targets = that file's sites in the
slices), `ledger_rows` (prior route rows for the cluster). The recorded test
builds the candidate runs from it and asserts the plan's sentence about the
order, with the fixed pre-schedule walk as the negative control.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _events(path: Path, run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run_id not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if str(ev.get("run_id", "")) != run_id:
                continue
            out.append(ev)
    return out


def _kind(ev: dict[str, Any]) -> str:
    return str(ev.get("event") or ev.get("kind") or "")


def _site(label: str) -> dict[str, str]:
    """`src/error.rs::fmt (impl Debug for Error)` -> file, fn, container."""
    head, _, cont = label.partition(" (")
    file, _, fn = head.partition("::")
    return {"file": file, "fn": fn, "container": cont.rstrip(")")}


def distil(run_id: str, iter_n: int, stubs: list[str], *,
           events_path: Path) -> dict[str, Any]:
    evs = _events(events_path, run_id)
    iters = [e for e in evs if _kind(e) == "phase_c.tdd.iter"]
    # The fill of iteration N starts at the N-th `phase_c.tdd.iter` and ends
    # at the next one (or the end of the run).
    starts = [i for i, e in enumerate(evs) if _kind(e) == "phase_c.tdd.iter"
              and int((e.get("data") or {}).get("iter", -1)) == iter_n]
    if not starts:
        raise SystemExit(f"no phase_c.tdd.iter {iter_n} for run {run_id} "
                         f"({len(iters)} iteration event(s) found)")
    start = starts[-1]
    end = next((i for i in range(start + 1, len(evs))
                if _kind(evs[i]) == "phase_c.tdd.iter"), len(evs))
    fill = evs[start:end]
    cluster = str((evs[start].get("data") or {}).get("cluster", ""))
    plan = next((e for e in fill if _kind(e) == "phase_c.session.plan"), None)
    if plan is None:
        raise SystemExit(f"no phase_c.session.plan inside iteration {iter_n}")
    pd = plan.get("data") or {}
    tests = {t: [_site(s) for s in sites]
             for t, sites in dict(pd.get("slices") or {}).items()}
    sessions = [{"tests": list(s.get("tests") or []), "units": int(s.get("units", 0))}
                for s in (pd.get("sessions") or [])]
    sched = next((e for e in fill if _kind(e) == "phase_c.fill.schedule"), None)
    file_runs: list[dict[str, Any]] = []
    stubs_source = "argument"
    stub_set = set(stubs)
    if sched is not None:
        sd = sched.get("data") or {}
        for r in list(sd.get("runs") or []) + list(sd.get("deferred") or []):
            if r.get("kind") == "file":
                file_runs.append({"file": (r.get("files") or [""])[0],
                                  "targets": list(r.get("targets") or []),
                                  "stubs": list(r.get("stubs") or [])})
            for st in r.get("stubs") or []:
                stub_set.add(str(st))
        if stub_set:
            stubs_source = "schedule_event"
    if not file_runs:
        by_file: dict[str, set[str]] = defaultdict(set)
        for sites in tests.values():
            for s in sites:
                by_file[s["file"]].add(f"{s['file']}::{s['fn']}")
        for f, locs in sorted(by_file.items()):
            f_stubs = sorted(loc for loc in locs if loc in stub_set)
            if f_stubs:
                file_runs.append({"file": f, "targets": sorted(locs),
                                  "stubs": f_stubs})
    ledger_rows = [e.get("data") or {} for e in evs[:start]
                   if _kind(e) == "phase_c.route.run"
                   and str((e.get("data") or {}).get("cluster", "")) == cluster]
    return {
        "run_id": run_id, "iter": iter_n, "cluster": cluster,
        "ts": str(plan.get("ts", "")),
        "tests": tests, "sessions": sessions,
        "hollow_recorded": int(pd.get("hollow", 0) or 0),
        "fitted_recorded": int(pd.get("fitted", 0) or 0),
        "stubs": sorted(stub_set), "stubs_source": stubs_source,
        "file_runs": file_runs, "ledger_rows": ledger_rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--stubs", default="",
                    help="comma-separated file::fn locators assumed hollow")
    ap.add_argument("--events", type=Path, default=REPO / "logs" / "events.jsonl")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args(argv)
    stubs = [s.strip() for s in args.stubs.split(",") if s.strip()]
    fixture = distil(args.run, args.iter, stubs, events_path=args.events)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"wrote {args.out}: {len(fixture['tests'])} test(s), "
          f"{len(fixture['sessions'])} session(s), {len(fixture['file_runs'])} "
          f"file run(s), {len(fixture['stubs'])} stub(s) "
          f"({fixture['stubs_source']}), hollow recorded "
          f"{fixture['hollow_recorded']}, {len(fixture['ledger_rows'])} "
          "ledger row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
