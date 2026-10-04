"""Is this code's phase cost different from the previous codes'?

The question a session used to answer by hand ("did t0c get slower after this
change?") as a pure function over the stored runs and phase samples
(:mod:`pipeline_core.domain.phase_cost`).

Comparable means the same crate, the same configuration fingerprint and the
same starting tree (the run's `origin`), and a phase whose every sample in
that run closed `ok`. A changed `.env` knob or a run resumed from a different
restore point is never reported as a code regression: it is
`NOT_COMPARABLE`, with the field that differs.

The statistic, per phase key and metric:

* a run's value is the SUM over its samples of that key (a phase entered
  twice in one run cost both);
* an identity's value is the median over its runs;
* the baseline is the previous `window` distinct identities (ordered by their
  first run) — their median, and their median absolute deviation;
* a metric moved when `|current - median|` exceeds the band
  `max(3 × MAD, 20 % × median, floor)`. The relative term keeps a MAD of 0
  on a deterministic phase from flagging one extra call; the per-metric floor
  keeps a 0 → 1 move on a phase that barely works from flagging at all.

Work metrics (LLM calls, tokens, cargo invocations) decide `REGRESSED` and
`IMPROVED`. Wall time that moved with flat work is `WALL_ONLY`, lower
severity: GPU contention and vLLM cache warmth move wall time without the code
changing. Fewer than `min_baseline` identities is `TOO_FEW` — the honest
reading for the first weeks of a store, not a pass.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median

from .phase_cost import PhaseKey, PhaseWork
from .run_health import HealthFinding, HealthKind, Severity

DEFAULT_WINDOW = 5
DEFAULT_MIN_BASELINE = 3
_MAD_K = 3.0
_REL = 0.20


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    read: Callable[[PhaseWork, int], float]
    floor: float          # smallest absolute move that can count
    work: bool            # decides REGRESSED/IMPROVED (else wall)
    unit: str = ""


METRICS: tuple[Metric, ...] = (
    Metric("llm calls", lambda w, _ms: float(w.llm_calls), 2.0, True),
    Metric("tokens", lambda w, _ms: float(w.tokens), 2000.0, True),
    Metric("cargo runs", lambda w, _ms: float(w.cargo), 2.0, True),
    Metric("wall", lambda _w, ms: ms / 1000.0, 5.0, False, "s"),
)


class Verdict(StrEnum):
    REGRESSED = "regressed"
    WALL_ONLY = "wall_only"
    IMPROVED = "improved"
    FLAT = "flat"
    NOT_COMPARABLE = "not_comparable"
    TOO_FEW = "too_few"


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    crate: str
    code_key: str | None
    config_fp: str | None
    origin: str | None
    started: str


@dataclass(frozen=True, slots=True)
class SampleRow:
    run_id: str
    key: PhaseKey
    outcome: str
    wall_ms: int
    work: PhaseWork


@dataclass(frozen=True, slots=True)
class MetricReading:
    metric: str
    unit: str
    current: float
    median: float
    band: float

    @property
    def delta(self) -> float:
        return self.current - self.median

    @property
    def moved_up(self) -> bool:
        return self.delta > self.band

    @property
    def moved_down(self) -> bool:
        return -self.delta > self.band

    def describe(self) -> str:
        pct = (f" {self.delta / self.median * 100:+.0f}%" if self.median
               else "")
        return (f"{self.metric}{pct} ({_fmt(self.median)}{self.unit} → "
                f"{_fmt(self.current)}{self.unit})")


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or v == int(v) else f"{v:.1f}"


@dataclass(frozen=True, slots=True)
class PhaseVerdict:
    key: PhaseKey
    verdict: Verdict
    readings: tuple[MetricReading, ...]
    baseline_identities: int
    current_runs: int
    # Per-identity medians, oldest first, the current identity last.
    calls_series: tuple[float, ...]
    wall_series: tuple[float, ...]
    reason: str = ""

    def moved(self) -> tuple[MetricReading, ...]:
        return tuple(r for r in self.readings if r.moved_up or r.moved_down)


@dataclass(frozen=True, slots=True)
class OpenPhase:
    """A phase the live run has entered and not left."""

    key: PhaseKey
    elapsed_ms: int


# ── grouping ────────────────────────────────────────────────────────────────


def _comparable(run: RunRecord, current: RunRecord) -> bool:
    return (run.crate == current.crate and run.config_fp == current.config_fp
            and run.origin == current.origin and run.code_key is not None)


def _per_run_values(samples: Iterable[SampleRow]
                    ) -> dict[PhaseKey, dict[str, tuple[PhaseWork, int]]]:
    """key -> run_id -> (summed work, summed wall). A run with any non-ok
    sample of a key is dropped for that key: its cost is a failed attempt's."""
    acc: dict[PhaseKey, dict[str, tuple[PhaseWork, int]]] = defaultdict(dict)
    bad: set[tuple[PhaseKey, str]] = set()
    for s in samples:
        if s.outcome != "ok":
            bad.add((s.key, s.run_id))
            continue
        work, wall = acc[s.key].get(s.run_id, (PhaseWork(), 0))
        work = work.copy()
        work.add(s.work)
        acc[s.key][s.run_id] = (work, wall + s.wall_ms)
    for key, run_id in bad:
        acc.get(key, {}).pop(run_id, None)
    return acc


def _identity_order(runs: Iterable[RunRecord]) -> list[str]:
    first: dict[str, str] = {}
    for r in runs:
        if r.code_key is None:
            continue
        if r.code_key not in first or r.started < first[r.code_key]:
            first[r.code_key] = r.started
    return sorted(first, key=lambda k: first[k])


def _identity_value(metric: Metric, run_ids: Iterable[str],
                    per_run: Mapping[str, tuple[PhaseWork, int]]) -> float | None:
    vals = [metric.read(*per_run[r]) for r in run_ids if r in per_run]
    return float(median(vals)) if vals else None


def _band(metric: Metric, values: Sequence[float]) -> tuple[float, float]:
    med = float(median(values))
    mad = float(median([abs(v - med) for v in values]))
    return med, max(_MAD_K * mad, _REL * abs(med), metric.floor)


def _differs(current: RunRecord, runs: Iterable[RunRecord],
             key: PhaseKey, per_key: Mapping[PhaseKey, Mapping[str, object]]) -> str:
    """Why runs of other identities that ran `key` are not comparable."""
    other = [r for r in runs
             if r.crate == current.crate and r.code_key != current.code_key
             and r.run_id in per_key.get(key, {})]
    if not other:
        return ""
    if all(r.code_key is None for r in other):
        return f"{len(other)} older run(s) have no code identity (backfilled)"
    if not any(r.config_fp == current.config_fp for r in other):
        return f"configuration differs from all {len(other)} other run(s)"
    if not any(r.origin == current.origin for r in other):
        return f"starting tree differs from all {len(other)} other run(s)"
    return f"configuration or starting tree differs from all {len(other)} other run(s)"


# ── the verdict ─────────────────────────────────────────────────────────────


def judge_run(current: RunRecord, runs: Sequence[RunRecord],
              samples: Sequence[SampleRow], *, window: int = DEFAULT_WINDOW,
              min_baseline: int = DEFAULT_MIN_BASELINE) -> list[PhaseVerdict]:
    """One verdict per phase key the current run closed `ok`."""
    per_key = _per_run_values(samples)
    keys = sorted((k for k, v in per_key.items() if current.run_id in v),
                  key=lambda k: (k.phase, k.module, k.cluster))
    if current.code_key is None:
        return [PhaseVerdict(k, Verdict.NOT_COMPARABLE, (), 0, 1, (), (),
                             "this run has no code identity") for k in keys]
    comparable = [r for r in runs if _comparable(r, current)]
    if all(r.run_id != current.run_id for r in comparable):
        comparable.append(current)
    by_identity: dict[str, list[str]] = defaultdict(list)
    for r in comparable:
        by_identity[str(r.code_key)].append(r.run_id)
    order = _identity_order(comparable)
    pos = order.index(current.code_key)
    out: list[PhaseVerdict] = []
    for key in keys:
        per_run = per_key[key]
        prior = [i for i in order[:pos]
                 if any(r in per_run for r in by_identity[i])][-window:]
        current_runs = [r for r in by_identity[current.code_key] if r in per_run]
        calls_series, wall_series = [], []
        for ident in [*prior, current.code_key]:
            c = _identity_value(METRICS[0], by_identity[ident], per_run)
            w = _identity_value(METRICS[-1], by_identity[ident], per_run)
            calls_series.append(c if c is not None else 0.0)
            wall_series.append(w if w is not None else 0.0)
        if not prior:
            why = _differs(current, runs, key, per_key)
            verdict = Verdict.NOT_COMPARABLE if why else Verdict.TOO_FEW
            out.append(PhaseVerdict(key, verdict, (), 0, len(current_runs),
                                    tuple(calls_series), tuple(wall_series),
                                    why or "no earlier code has run this phase"))
            continue
        readings: list[MetricReading] = []
        for m in METRICS:
            base = [v for v in (_identity_value(m, by_identity[i], per_run)
                                for i in prior) if v is not None]
            cur = _identity_value(m, current_runs, per_run)
            if not base or cur is None:
                continue
            med, band = _band(m, base)
            readings.append(MetricReading(m.name, m.unit, cur, med, band))
        if len(prior) < min_baseline:
            verdict = Verdict.TOO_FEW
            reason = f"{len(prior)} earlier code identit(ies), {min_baseline} needed"
        else:
            work = [r for r in readings if _is_work(r.metric)]
            wall = [r for r in readings if not _is_work(r.metric)]
            if any(r.moved_up for r in work):
                verdict = Verdict.REGRESSED
            elif any(r.moved_up for r in wall):
                verdict = Verdict.WALL_ONLY
            elif any(r.moved_down for r in work) or any(r.moved_down for r in wall):
                verdict = Verdict.IMPROVED
            else:
                verdict = Verdict.FLAT
            reason = ""
        out.append(PhaseVerdict(key, verdict, tuple(readings), len(prior),
                                len(current_runs), tuple(calls_series),
                                tuple(wall_series), reason))
    return out


def _is_work(metric_name: str) -> bool:
    return any(m.work for m in METRICS if m.name == metric_name)


def baseline_wall_p90(current: RunRecord, runs: Sequence[RunRecord],
                      samples: Sequence[SampleRow], key: PhaseKey, *,
                      window: int = DEFAULT_WINDOW,
                      min_baseline: int = DEFAULT_MIN_BASELINE) -> float | None:
    """The 90th-percentile wall (ms) of `key` over the comparable runs of the
    previous `window` identities, or None with fewer than `min_baseline`."""
    if current.code_key is None:
        return None
    per_run = _per_run_values(s for s in samples if s.key == key).get(key, {})
    comparable = [r for r in runs if _comparable(r, current)
                  and r.code_key != current.code_key and r.run_id in per_run]
    order = _identity_order(comparable)[-window:]
    walls = sorted(float(per_run[r.run_id][1]) for r in comparable
                   if r.code_key in order)
    if len(order) < min_baseline or not walls:
        return None
    idx = min(len(walls) - 1, int(round(0.9 * (len(walls) - 1))))
    return walls[idx]


# ── health findings ─────────────────────────────────────────────────────────


def findings_of(verdicts: Iterable[PhaseVerdict]) -> list[HealthFinding]:
    out: list[HealthFinding] = []
    for v in verdicts:
        if v.verdict not in (Verdict.REGRESSED, Verdict.WALL_ONLY):
            continue
        moved = v.moved() or v.readings
        evidence = (", ".join(r.describe() for r in moved)
                    + f" vs {v.baseline_identities} earlier code identit(ies)")
        if v.verdict is Verdict.REGRESSED:
            out.append(HealthFinding(
                HealthKind.PHASE_COST_REGRESSION, Severity.ALERT, v.key.label(),
                evidence,
                "this code buys more work for the same phase on the same config "
                "and starting tree — diff the change against the previous "
                "identity (tools/perf_ledger.py phases)"))
        else:
            out.append(HealthFinding(
                HealthKind.PHASE_COST_REGRESSION, Severity.WARN, v.key.label(),
                evidence + " — work is flat",
                "wall moved without the work moving: check GPU contention and "
                "the vLLM cache before blaming the code"))
    return out


def overrun_findings(current: RunRecord, runs: Sequence[RunRecord],
                     samples: Sequence[SampleRow],
                     open_phases: Iterable[OpenPhase]) -> list[HealthFinding]:
    """A phase still running past its baseline's p90 wall — the alert that
    does not wait for the phase to end."""
    out: list[HealthFinding] = []
    for op in open_phases:
        p90 = baseline_wall_p90(current, runs, samples, op.key)
        if p90 is None or op.elapsed_ms <= p90:
            continue
        out.append(HealthFinding(
            HealthKind.PHASE_OVERRUN, Severity.WARN, op.key.label(),
            f"running {op.elapsed_ms / 60000:.1f}m, past the earlier codes' "
            f"p90 of {p90 / 60000:.1f}m on this config and starting tree",
            "look at what this phase is buying right now (Stream / Events "
            "view) before it finishes"))
    return out
