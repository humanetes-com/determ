#!/usr/bin/env python3
"""Seed `logs/run_stats.db` with the phase history recorded before the
pipeline wrote it itself.

WHY THIS EXISTS
  The pipeline records each phase's cost per run from launch
  (`adapters/run_stats_recorder.py`). Everything before that lives in two
  places: `logs/phases.jsonl` (every `phase.exit` with its `duration_ms`,
  keyed by run id and the git HEAD of the time) and `logs/perf_ledger.jsonl`
  (run-level rows appended by hand). This script copies both into the store
  so `tools/perf_ledger.py phases --history <phase>` lists one history.

  What the old record cannot carry, and so these rows do not: a code
  identity (HEAD is the same across every uncommitted edit between two
  commits), the configuration, the starting tree, and per-phase work (no
  event named its phase). Backfilled rows therefore never enter a baseline:
  `judge_run` compares only runs with a code identity, the same config
  fingerprint and the same origin, and says `not_comparable` (backfilled)
  instead of guessing.

  The sweeps (`domain.sweep_trajectory`) — each TDD iteration's score, clock
  and model-call count — are read from `logs/events.jsonl`, whose
  `phase_c.tdd.iter` events carry them for every run since the event grew a
  `pass_count`. They are what the health tab's score-climb section and
  `tools/perf_ledger.py sweeps` render; a run with no row yet is opened as
  `backfill` with HEAD from the event's `pipeline_sha`.

USAGE
  .venv/bin/python scripts/backfill_run_stats.py [--logs logs] [--dry-run]

  Idempotent: a run id already in the store is skipped for phases, and a
  sweep already stored (same run, cluster, timestamp) is skipped, so a live
  run is never overwritten and a second backfill adds nothing. Re-running it
  during a run recorded by older code imports that run's newer sweeps.
  Streams the whole event log once (350 MB on 2026-10-15, about a minute).

HOW TO INTERPRET
  `sweeps: N row(s) from M run(s)` is what the score-climb view gained.
  The summary then prints, per crate, the runs and `ok` phase samples imported,
  then the per-phase `ok` sample count and median wall over the imported
  rows, per HEAD. A phase with one or two samples per HEAD is the reason the
  record could not answer "did this change make t0c slower" by itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.run_stats_store import (  # noqa: E402
    SQLiteRunStatsStore,
    run_stats_path,
)
from pipeline_core.domain.phase_cost import (  # noqa: E402
    PhaseKey,
    PhaseSample,
    PhaseWork,
    RunConfig,
    work_delta,
)
from pipeline_core.domain.sweep_trajectory import (  # noqa: E402
    SWEEP_EVENT,
    SweepRow,
    sweep_of,
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _started(exit_ts: str, duration_ms: int) -> str:
    try:
        t = datetime.fromisoformat(exit_ts) - timedelta(milliseconds=duration_ms)
    except ValueError:
        return exit_ts
    return t.isoformat(timespec="milliseconds")


def _run_work(events: Path, run_ids: set[str]) -> dict[str, PhaseWork]:
    """Whole-run work from events.jsonl (rows that carry a run id)."""
    work: dict[str, PhaseWork] = defaultdict(PhaseWork)
    if not events.exists():
        return work
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"run_id"' not in line or '"llm.' not in line and '"cargo.' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("run_id")
            if rid not in run_ids:
                continue
            delta = work_delta(rec)
            if delta is not None:
                work[rid].add(delta)
    return work


def _sweeps(events: Path) -> tuple[list[SweepRow], dict[str, dict[str, str]]]:
    """Every measured sweep in events.jsonl, with each run's start, crate and
    HEAD from its `pipeline.start`. The model-call count at a sweep is the
    `llm.request` events of that run seen before it, as the live recorder
    counts them."""
    rows: list[SweepRow] = []
    runs: dict[str, dict[str, str]] = {}
    calls: dict[str, int] = defaultdict(int)
    if not events.exists():
        return rows, runs
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"run_id"' not in line:
                continue
            is_req = '"llm.request"' in line
            if not (is_req or '"pipeline.start"' in line or f'"{SWEEP_EVENT}"' in line):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rid = rec.get("run_id")
            if not isinstance(rid, str) or not rid:
                continue
            ev = rec.get("event")
            if ev == "llm.request":
                calls[rid] += 1
            elif ev == "pipeline.start":
                runs.setdefault(rid, {"started": str(rec.get("ts") or ""),
                                      "crate": str(rec.get("product") or "unknown"),
                                      "head": str(rec.get("pipeline_sha") or "")})
            elif rid in runs:
                row = sweep_of(rec, run_id=rid, run_started=runs[rid]["started"],
                               llm_calls=calls[rid])
                if row is not None:
                    rows.append(row)
    return rows, runs


def _import_sweeps(store: SQLiteRunStatsStore | None, events: Path) -> None:
    rows, runs = _sweeps(events)
    have: set[tuple[str, str, str]] = set()
    if store:
        have = {(r.run_id, r.cluster, r.ts) for r in store.sweeps({r.run_id for r in rows})}
    fresh = [r for r in rows if (r.run_id, r.cluster, r.ts) not in have]
    for rid in sorted({r.run_id for r in fresh}):
        if store and not store.has_run(rid):
            meta = runs[rid]
            store.open_run(run_id=rid, crate=meta["crate"], code_key=None,
                           tree_stamp=None, commit=meta["head"] or None,
                           config_fp=None, config=None, started=meta["started"],
                           source="backfill")
    if store:
        for r in fresh:
            store.add_sweep(r, source="backfill")
    print(f"sweeps: {len(fresh)} row(s) from {len({r.run_id for r in fresh})} run(s) "
          f"imported from {events.name} ({len(rows) - len(fresh)} already stored)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logs", default=str(ROOT / "logs"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logs = Path(args.logs)
    phases = _jsonl(logs / "phases.jsonl")
    ledger = _jsonl(logs / "perf_ledger.jsonl")

    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in phases:
        rid = rec.get("run_id")
        if isinstance(rid, str) and rid:
            by_run[rid].append(rec)

    store = None if args.dry_run else SQLiteRunStatsStore(
        run_stats_path(logs / "events.jsonl"))
    known = {r.record.run_id for r in store.runs()} if store else set()
    new_runs = {rid for rid in by_run if rid not in known}
    work = _run_work(logs / "events.jsonl", new_runs)

    imported: dict[str, list[tuple[str, str, int]]] = defaultdict(list)  # crate -> (head, phase, wall)
    run_count: dict[str, int] = defaultdict(int)
    for rid in sorted(new_runs, key=lambda r: by_run[r][0].get("ts", "")):
        recs = by_run[rid]
        crate = str(recs[0].get("product") or "unknown")
        head = str(recs[0].get("pipeline_sha") or "") or None
        run_count[crate] += 1
        if store:
            store.open_run(run_id=rid, crate=crate, code_key=None, tree_stamp=None,
                           commit=head, config_fp=None, config=None,
                           started=str(recs[0].get("ts") or ""), source="backfill")
            if rid in work:
                store.set_run_work(rid, work[rid])
        for rec in recs:
            if rec.get("event") != "phase.exit" or rec.get("outcome") in (None, "skipped"):
                continue
            ms = int(rec.get("duration_ms") or 0)
            sample = PhaseSample(
                key=PhaseKey(str(rec.get("phase") or ""), str(rec.get("module") or ""),
                             str(rec.get("cluster") or "")),
                scope="", parent_scope=None, started=_started(str(rec.get("ts") or ""), ms),
                wall_ms=ms, outcome=str(rec.get("outcome")), work=PhaseWork())
            if store:
                store.add_sample(rid, sample, source="backfill")
            if sample.outcome == "ok":
                imported[crate].append((head or "?", sample.key.phase, ms))

    recorded = 0
    for row in ledger:
        rid = row.get("run_id")
        if not isinstance(rid, str) or rid in known or rid in new_runs:
            continue
        env = {k: str(v) for k, v in (row.get("env") or {}).items() if v is not None}
        cfg = RunConfig.from_env(env) if env else None
        code = row.get("code") or {}
        metrics = row.get("metrics") or {}
        recorded += 1
        if store:
            store.open_run(run_id=rid, crate=str(row.get("crate") or "unknown"),
                           code_key=None, tree_stamp=code.get("tree_stamp"),
                           commit=code.get("commit"),
                           config_fp=cfg.fingerprint if cfg else None,
                           config=cfg.as_dict() if cfg else None,
                           started=str(row.get("date") or ""), source="recorded",
                           note=row.get("note"))
            wall = metrics.get("wall_min")
            store.set_run_work(rid, PhaseWork(llm_calls=int(metrics.get("llm_calls") or 0)),
                               wall_ms=int(wall * 60000) if wall else None)

    _import_sweeps(store, logs / "events.jsonl")

    for crate in sorted(run_count):
        rows = imported[crate]
        print(f"{crate}: {run_count[crate]} run(s), {len(rows)} ok phase sample(s) "
              "imported from phases.jsonl")
        per: dict[tuple[str, str], list[int]] = defaultdict(list)
        for head, phase, ms in rows:
            per[(phase, head)].append(ms)
        for (phase, head), walls in sorted(per.items()):
            print(f"  {phase:<22s} HEAD {head:<13s} n={len(walls):<3d} "
                  f"median wall {median(walls) / 1000:8.1f}s")
    print(f"perf_ledger.jsonl: {recorded} run row(s) imported as `recorded`")
    if args.dry_run:
        print("(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
