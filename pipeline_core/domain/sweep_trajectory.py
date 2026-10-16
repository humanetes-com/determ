"""What each TDD sweep scored, and how a run's sweeps climbed.

The phase-cost record (:mod:`phase_cost_baseline`) answers "did this code make
a phase more expensive", and only for a phase that CLOSED. The fill phase
rarely closes inside one process — of the 28 live runs recorded 2026-10-04 →
10-15, one closed `t2a` — so it cannot answer the question a run is launched
for: did the score climb, in what order, and how fast. Every sweep already
prints that reading as a `phase_c.tdd.iter` event; this module is the one
reading of that event the live recorder and the backfill both take, and the
summary the health tab and `tools/perf_ledger.py sweeps` render from it.

A sweep's clock and call count are cumulative from the PROCESS start, so two
sweeps of one run subtract; the summary reports them from the cluster's first
sweep of the phase, which is the entry measurement before any fill.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SWEEP_EVENT = "phase_c.tdd.iter"


@dataclass(frozen=True, slots=True)
class SweepRow:
    run_id: str
    phase: str            # top-level phase the sweep ran in (`t2a`, `t2c`)
    cluster: str
    iter: int
    ts: str
    at_ms: int            # since the process started
    llm_calls: int        # model requests since the process started
    pass_count: int
    fail_count: int
    oracle_static: int    # tests the suite declares; the denominator
    frontier: int
    stall: int
    improved: bool


def _int(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _epoch(ts: object) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def sweep_of(record: Mapping[str, Any], *, run_id: str, run_started: str,
             llm_calls: int) -> SweepRow | None:
    """The sweep a `phase_c.tdd.iter` event reports, or None for any other
    event and for an iteration that measured nothing (no `pass_count`: the
    early no-frontier form, or an iteration that ended before its sweep)."""
    if record.get("event") != SWEEP_EVENT:
        return None
    data = record.get("data")
    if not isinstance(data, Mapping):
        return None
    passed = _int(data.get("pass_count"))
    it = _int(data.get("iter"))
    if passed is None or it is None:
        return None
    ts = str(record.get("ts") or "")
    t0, t1 = _epoch(run_started), _epoch(ts)
    at_ms = int(max(0.0, t1 - t0) * 1000) if t0 is not None and t1 is not None else 0
    in_phase = record.get("in_phase")
    return SweepRow(
        run_id=run_id,
        phase=in_phase.split(".", 1)[0] if isinstance(in_phase, str) else "",
        cluster=str(data.get("cluster") or record.get("cluster") or ""),
        iter=it, ts=ts, at_ms=at_ms, llm_calls=llm_calls, pass_count=passed,
        fail_count=_int(data.get("fail_count")) or 0,
        oracle_static=_int(data.get("oracle_static")) or 0,
        frontier=_int(data.get("frontier_size")) or 0,
        stall=_int(data.get("stall")) or 0,
        improved=bool(data.get("improved")))


# ── the climb ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Climb:
    """One cluster's sweeps in one phase of one run, in order."""

    phase: str
    cluster: str
    sweeps: tuple[SweepRow, ...]

    @property
    def first(self) -> SweepRow:
        return self.sweeps[0]

    @property
    def last(self) -> SweepRow:
        return self.sweeps[-1]

    @property
    def best(self) -> SweepRow:
        """The first sweep that reached the run's high-water."""
        top = max(s.pass_count for s in self.sweeps)
        return next(s for s in self.sweeps if s.pass_count == top)

    def since_entry(self, s: SweepRow) -> tuple[int, int]:
        """(ms, model calls) from the entry sweep to `s`."""
        return (max(0, s.at_ms - self.first.at_ms),
                max(0, s.llm_calls - self.first.llm_calls))

    @property
    def regressions(self) -> int:
        """Sweeps that read lower than the sweep before them."""
        return sum(1 for a, b in zip(self.sweeps, self.sweeps[1:], strict=False)
                   if b.pass_count < a.pass_count)

    def sequence(self) -> list[tuple[int, int]]:
        """(pass, repeats) with consecutive equal readings folded."""
        return [(p, n) for p, n, _ in self.steps()]

    def steps(self) -> list[tuple[int, int, int]]:
        """(pass, repeats, ms) with consecutive equal readings folded; `ms` is
        the time from the previous score's first sweep to this score's first
        sweep, so it covers the flat sweeps in between (0 for the entry)."""
        out: list[tuple[int, int, int]] = []
        since = self.first.at_ms
        for s in self.sweeps:
            if out and out[-1][0] == s.pass_count:
                p, n, ms = out[-1]
                out[-1] = (p, n + 1, ms)
            else:
                out.append((s.pass_count, 1, max(0, s.at_ms - since) if out else 0))
                since = s.at_ms
        return out


def climbs_of(rows: Iterable[SweepRow]) -> list[Climb]:
    """Group one run's sweeps by (phase, cluster), each in recorded order,
    the groups in the order their first sweep happened."""
    groups: dict[tuple[str, str], list[SweepRow]] = {}
    for r in rows:
        groups.setdefault((r.phase, r.cluster), []).append(r)
    climbs = [Climb(p, c, tuple(sorted(g, key=lambda s: (s.at_ms, s.iter))))
              for (p, c), g in groups.items()]
    return sorted(climbs, key=lambda c: c.first.at_ms)


def fmt_elapsed(ms: int) -> str:
    m = ms // 60000
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_sequence(seq: Sequence[tuple[int, int]] | Sequence[tuple[int, int, int]], *,
                 limit: int = 14) -> str:
    """`33→41→58×3→71`, or with step times `33 -1h05m→ 41 -40m→ 58×3`; the
    middle elided past `limit` readings, the entry and the latest kept."""
    parts: list[tuple[str, int | None]] = []
    for step in seq:
        p, n = step[0], step[1]
        gap = step[2] if len(step) == 3 else None
        parts.append((f"{p}×{n}" if n > 1 else str(p), gap))
    if len(parts) > limit:
        head = limit // 2
        parts = [*parts[:head], (f"…{len(parts) - limit}…", None),
                 *parts[-(limit - head):]]
    text = parts[0][0] if parts else ""
    for label, gap in parts[1:]:
        text += f" -{fmt_elapsed(gap)}→ {label}" if gap is not None else f"→{label}"
    return text
