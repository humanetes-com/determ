"""The recent runs' score climbs, for the health tab and the ledger tool.

Reads the `sweeps` table of `run_stats.db` (read-only) and renders, for each
of the last few runs that measured a sweep, newest first: its code identity
and HEAD, the phase it entered at, and per (phase, cluster) the sequence of
sweep scores with the high-water and how long and how many model calls the
climb from the entry sweep to it took.

`tools/perf_ledger.py sweeps` prints the same lines from the same function.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.run_stats_store import (
    SQLiteRunStatsStore,
    StoredRun,
    run_stats_path,
)
from pipeline_core.domain.sweep_trajectory import (
    Climb,
    SweepRow,
    climbs_of,
    fmt_elapsed,
    fmt_sequence,
)

DEFAULT_RUNS = 6


def _climb_line(c: Climb) -> str:
    best = c.best
    ms, calls = c.since_entry(best)
    where = f"{c.phase}/{c.cluster}" if c.phase else c.cluster
    reached = (f"best {best.pass_count}/{best.oracle_static} at sweep {best.iter} "
               f"(+{fmt_elapsed(ms)}, {calls} calls)"
               if best is not c.first else
               f"best {best.pass_count}/{best.oracle_static} = entry")
    tail = [f"{len(c.sweeps)} sweep(s)", reached]
    last_ms, _ = c.since_entry(c.last)
    if c.last is not best:
        tail.append(f"last {c.last.pass_count} stall={c.last.stall} "
                    f"(+{fmt_elapsed(last_ms)})")
    if c.regressions:
        tail.append(f"{c.regressions} drop(s)")
    return f"    {where:<14} {fmt_sequence(c.steps())}   " + " · ".join(tail)


def _run_head(run: StoredRun, entered: str) -> str:
    rec = run.record
    state = "unfinished" if run.ended is None else (run.outcome or "ended")
    bits = [rec.run_id, f"{rec.started[:16].replace('T', ' ')}",
            f"code={(rec.code_key or '?')[:8]}"]
    if run.commit:
        bits.append(f"HEAD={run.commit[:7]}")
    bits += [f"entered {entered}", state]
    if run.source != "live":
        bits.append(run.source)
    return "  " + "  ".join(bits)


def sweep_lines(events_log: str | Path, *, runs: int = DEFAULT_RUNS,
                crate: str | None = None) -> list[str]:
    path = run_stats_path(events_log)
    title = f"Score climb — last {runs} run(s) that swept, newest first"
    if not path.exists():
        return [title, f"  no {path.name} yet"]
    store = SQLiteRunStatsStore(path, read_only=True)
    try:
        recent = store.runs_with_sweeps(crate, limit=runs)
        ids = [r.record.run_id for r in recent]
        rows = store.sweeps(ids)
        entered = store.entry_phases(ids)
    finally:
        store.close()
    if not recent:
        return [title, "  no sweep recorded yet — a run records one per TDD "
                "iteration; `scripts/backfill_run_stats.py` imports past runs"]
    by_run: dict[str, list[SweepRow]] = {}
    for r in rows:
        by_run.setdefault(r.run_id, []).append(r)
    out = [title,
           "  per (phase/cluster): pass per sweep, equal sweeps folded as ×n, "
           "-time→ from the previous score's first sweep; +time and calls from "
           "the entry sweep"]
    for run in recent:
        rid = run.record.run_id
        out.append(_run_head(run, entered.get(rid, "?")))
        out += [_climb_line(c) for c in climbs_of(by_run.get(rid, []))]
    return out

