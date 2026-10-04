"""The plan's sentence: a phase whose LLM calls rose 40% over five prior
comparable code identities is flagged; a resumed or differently configured
phase is not compared."""
from __future__ import annotations

from pipeline_core.domain.phase_cost import PhaseKey, PhaseWork
from pipeline_core.domain.phase_cost_baseline import (
    OpenPhase,
    RunRecord,
    SampleRow,
    Verdict,
    findings_of,
    judge_run,
    overrun_findings,
)
from pipeline_core.domain.run_health import HealthKind, Severity

T0C = PhaseKey("t0c", "anyhow", "stable")


def _history(calls: list[int], walls: list[int], *, config="cfg", origin="clone"):
    """One run per identity, identities c0.. in time order."""
    runs, samples = [], []
    for i, (c, w) in enumerate(zip(calls, walls, strict=True)):
        rid = f"r{i}"
        runs.append(RunRecord(rid, "anyhow", f"c{i}", config, origin,
                              f"2026-10-0{1 + i // 10}T{i % 10:02d}:00:00"))
        samples.append(SampleRow(rid, T0C, "ok", w, PhaseWork(llm_calls=c)))
    return runs, samples


def _verdict(runs, samples, current=None):
    cur = current or runs[-1]
    [v] = judge_run(cur, runs, samples)
    return v


def test_calls_up_40_percent_over_five_prior_identities_is_regressed() -> None:
    runs, samples = _history([100, 102, 98, 101, 99, 140],
                             [600_000] * 6)
    v = _verdict(runs, samples)
    assert v.verdict is Verdict.REGRESSED
    assert v.baseline_identities == 5
    moved = {r.metric for r in v.moved()}
    assert moved == {"llm calls"}
    [f] = findings_of([v])
    assert (f.kind, f.severity) == (HealthKind.PHASE_COST_REGRESSION, Severity.ALERT)
    assert "llm calls +40% (100 → 140)" in f.evidence


def test_same_calls_with_40_percent_more_wall_is_wall_only() -> None:
    runs, samples = _history([100] * 6, [600_000, 610_000, 590_000, 605_000,
                                         595_000, 840_000])
    v = _verdict(runs, samples)
    assert v.verdict is Verdict.WALL_ONLY
    [f] = findings_of([v])
    assert f.severity is Severity.WARN and "work is flat" in f.evidence


def test_ordinary_spread_is_flat() -> None:
    runs, samples = _history([100, 104, 96, 101, 99, 110], [600_000] * 6)
    assert _verdict(runs, samples).verdict is Verdict.FLAT
    assert findings_of([_verdict(runs, samples)]) == []


def test_a_resumed_run_is_not_compared_with_fresh_ones() -> None:
    runs, samples = _history([100, 102, 98, 101, 99], [600_000] * 5)
    resumed = RunRecord("rx", "anyhow", "cX", "cfg", "restored-tag", "2026-10-02T00:00")
    v = _verdict([*runs, resumed],
                 [*samples, SampleRow("rx", T0C, "ok", 5_000, PhaseWork(llm_calls=3))],
                 current=resumed)
    assert v.verdict is Verdict.NOT_COMPARABLE
    assert "starting tree differs" in v.reason
    assert findings_of([v]) == []


def test_a_changed_knob_is_not_reported_as_a_code_regression() -> None:
    runs, samples = _history([100, 102, 98, 101, 99], [600_000] * 5)
    other = RunRecord("ry", "anyhow", "cY", "other-cfg", "clone", "2026-10-02T00:00")
    v = _verdict([*runs, other],
                 [*samples, SampleRow("ry", T0C, "ok", 900_000, PhaseWork(llm_calls=300))],
                 current=other)
    assert v.verdict is Verdict.NOT_COMPARABLE
    assert "configuration differs" in v.reason


def test_two_runs_on_one_identity_do_not_flag_each_other() -> None:
    a = RunRecord("a", "anyhow", "c0", "cfg", "clone", "2026-10-01T00:00")
    b = RunRecord("b", "anyhow", "c0", "cfg", "clone", "2026-10-01T05:00")
    samples = [SampleRow("a", T0C, "ok", 600_000, PhaseWork(llm_calls=100)),
               SampleRow("b", T0C, "ok", 900_000, PhaseWork(llm_calls=200))]
    v = _verdict([a, b], samples, current=b)
    assert v.verdict is Verdict.TOO_FEW
    assert v.current_runs == 2


def test_fewer_than_three_earlier_identities_is_too_few_not_a_pass() -> None:
    runs, samples = _history([100, 100, 300], [600_000] * 3)
    v = _verdict(runs, samples)
    assert v.verdict is Verdict.TOO_FEW
    assert "2 earlier code identit(ies), 3 needed" in v.reason


def test_a_run_with_a_failed_attempt_of_the_phase_is_left_out_for_it() -> None:
    runs, samples = _history([100, 102, 98, 101, 99, 100], [600_000] * 6)
    # Earlier identity c2 also errored once: its t0c cost is a failed attempt's.
    samples.append(SampleRow("r2", T0C, "error", 1_000, PhaseWork(llm_calls=900)))
    v = _verdict(runs, samples)
    assert v.verdict is Verdict.FLAT
    assert v.baseline_identities == 4


def test_a_phase_entered_twice_in_a_run_costs_both() -> None:
    runs, samples = _history([100, 102, 98, 101, 99, 70], [600_000] * 6)
    samples.append(SampleRow("r5", T0C, "ok", 100_000, PhaseWork(llm_calls=70)))
    v = _verdict(runs, samples)
    assert v.verdict is Verdict.REGRESSED       # 70 + 70 = 140


def test_a_run_without_a_code_identity_is_not_compared() -> None:
    runs, samples = _history([100] * 4, [600_000] * 4)
    old = RunRecord("bk", "anyhow", None, None, None, "2026-09-01T00:00")
    v = _verdict([old, *runs],
                 [SampleRow("bk", T0C, "ok", 1, PhaseWork()), *samples], current=old)
    assert v.verdict is Verdict.NOT_COMPARABLE


def test_a_phase_still_running_past_the_baseline_p90_is_a_finding() -> None:
    runs, samples = _history([100] * 5, [600_000, 620_000, 640_000, 610_000, 630_000])
    live = RunRecord("live", "anyhow", "cL", "cfg", "clone", "2026-10-03T00:00")
    runs.append(live)
    over = overrun_findings(live, runs, samples, [OpenPhase(T0C, 700_000)])
    assert [f.kind for f in over] == [HealthKind.PHASE_OVERRUN]
    assert overrun_findings(live, runs, samples, [OpenPhase(T0C, 500_000)]) == []
