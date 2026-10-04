"""Phase cost against the earlier code, for the health tab and the ledger tool.

Reads `run_stats.db` beside the event log (read-only; the pipeline is its only
writer) and the live run's open phases from `phases.jsonl`, and renders:

* findings — a phase this code made more expensive
  (`phase_cost_baseline.findings_of`) and a phase still running past the
  earlier codes' p90 wall (`overrun_findings`), in the run-health shape;
* the table — every phase this run closed, its verdict, calls and wall against
  the baseline median, and a sparkline of calls (wall when the phase makes no
  calls) over the earlier identities with this one last.

`tools/perf_ledger.py phases` prints the same table from the same functions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pipeline_core.adapters.run_stats_store import (
    SQLiteRunStatsStore,
    StoredRun,
    run_stats_path,
)
from pipeline_core.domain.phase_cost import PhaseKey
from pipeline_core.domain.phase_cost_baseline import (
    DEFAULT_WINDOW,
    OpenPhase,
    PhaseVerdict,
    Verdict,
    findings_of,
    judge_run,
    overrun_findings,
)
from pipeline_core.domain.phase_registry import PHASE_ENTER, PHASE_EXIT
from pipeline_core.domain.run_health import HealthFinding
from pipeline_core.monitor import run_data

_SPARK = "▁▂▃▄▅▆▇█"
_MARK = {Verdict.REGRESSED: "‼", Verdict.WALL_ONLY: "⚠", Verdict.IMPROVED: "↓",
         Verdict.FLAT: "·", Verdict.NOT_COMPARABLE: "≠", Verdict.TOO_FEW: "…"}


def sparkline(values: tuple[float, ...]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[0] * len(values)
    return "".join(_SPARK[int((v - lo) / (hi - lo) * (len(_SPARK) - 1))] for v in values)


def _short(v: float) -> str:
    if v >= 10_000:
        return f"{v / 1000:.0f}k"
    return f"{v:.0f}" if v >= 10 or v == int(v) else f"{v:.1f}"


def render_table(run: StoredRun, verdicts: list[PhaseVerdict], *,
                 window: int = DEFAULT_WINDOW) -> list[str]:
    rec = run.record
    head = [f"Phase cost vs the previous {window} code identities — run {rec.run_id} "
            f"({rec.crate})",
            f"  code={rec.code_key or '?'}  config={rec.config_fp or '?'}  "
            f"origin={(rec.origin or '?')[:12]}"
            + (f"  stamp={run.tree_stamp}" if run.tree_stamp else "")
            + (f"  HEAD={run.commit}" if run.commit else "")]
    if not verdicts:
        return [*head, "  no phase of this run has closed yet"]
    rows = []
    for v in verdicts:
        by = {r.metric: r for r in v.readings}
        calls, wall = by.get("llm calls"), by.get("wall")
        cur_calls = (calls.current if calls else (v.calls_series[-1] if v.calls_series else 0.0))
        cur_wall = (wall.current if wall else (v.wall_series[-1] if v.wall_series else 0.0))
        calls_s = _short(cur_calls) + (f" ({_short(calls.median)})" if calls else "")
        wall_s = _short(cur_wall) + "s" + (f" ({_short(wall.median)}s)" if wall else "")
        series = v.calls_series if any(v.calls_series) else v.wall_series
        tail = v.reason or ", ".join(r.describe() for r in v.moved())
        rows.append(f"  {_MARK[v.verdict]} {v.key.label():<30.30} {v.verdict.value:<14} "
                    f"calls {calls_s:<12} wall {wall_s:<16} "
                    f"{sparkline(series):<{window + 1}} {tail}".rstrip())
    legend = ("  ‼ regressed  ⚠ wall only  ↓ improved  · flat  ≠ not comparable  "
              "… too few earlier identities   (n) = baseline median")
    return [*head, *rows, legend]


@dataclass(frozen=True, slots=True)
class PhaseCostReport:
    findings: list[HealthFinding]
    table: list[str]


def _epoch(ts: object) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def open_phases(records: list[dict[str, object]], run_id: str, now: float) -> list[OpenPhase]:
    """Phases of `run_id` entered and not exited, with their elapsed time."""
    entered: dict[str, dict[str, object]] = {}
    for r in records:
        if r.get("run_id") != run_id or not isinstance(r.get("scope"), str):
            continue
        sid = str(r["scope"])
        if r.get("event") == PHASE_ENTER:
            entered[sid] = r
        elif r.get("event") == PHASE_EXIT:
            entered.pop(sid, None)
    out = []
    for r in entered.values():
        t0 = _epoch(r.get("ts"))
        if t0 is None:
            continue
        out.append(OpenPhase(PhaseKey(str(r.get("phase") or ""), str(r.get("module") or ""),
                                      str(r.get("cluster") or "")),
                             int(max(0.0, now - t0) * 1000)))
    return out


def phase_cost_report(events_log: str | Path, *, running: bool,
                      now: float | None = None,
                      run_id: str | None = None) -> PhaseCostReport:
    """Findings and table for `run_id` (default: the latest live run)."""
    path = run_stats_path(events_log)
    if not path.exists():
        return PhaseCostReport([], [f"Phase cost: no {path.name} yet — the first "
                                    "run on this code writes it"])
    store = SQLiteRunStatsStore(path, read_only=True)
    try:
        run = store.run(run_id) if run_id else store.latest_run()
        if run is None:
            return PhaseCostReport([], ["Phase cost: no recorded run"
                                        + (f" {run_id}" if run_id else "")])
        runs = store.runs(run.record.crate)
        samples = store.samples([r.record.run_id for r in runs])
    finally:
        store.close()
    records = [r.record for r in runs]
    verdicts = judge_run(run.record, records, samples)
    findings = findings_of(verdicts)
    if running and run.ended is None:
        live = open_phases(run_data.read_phase_records(str(events_log)),
                           run.record.run_id, time.time() if now is None else now)
        findings += overrun_findings(run.record, records, samples, live)
    return PhaseCostReport(findings, render_table(run, verdicts))
