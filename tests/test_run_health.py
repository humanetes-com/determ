"""Pins for the hourly run-health detectors (pipeline_core/domain/run_health).
Each detector encodes an issue class found once by manual deep analysis and
now detected automatically (user directive 2026-07-10)."""
from __future__ import annotations

from pipeline_core.domain.run_health import (
    HealthKind,
    analyze,
    parse_iter_readings,
)

ITER = ("  [stable] [phase-c/tdd] iter 2: cargo test rc=101 (0.1s); pass=14 "
        "fail=54 (improved=False, stall=1/3); frontier=31 fn(s) (merged)")


def _kinds(findings):
    return {f.kind for f in findings}


def test_parse_iter_readings():
    r = parse_iter_readings([ITER])
    assert len(r) == 1 and r[0].pass_count == 14 and r[0].fail_count == 54
    assert r[0].stall == 1 and r[0].stall_max == 3


def test_poisoned_notes_detected():
    notes = ["[regression] x (cluster pass count dropped 22→22). WRONG",
             "I tried X because Y"]
    f = analyze([], notes)
    assert HealthKind.POISONED_NOTES in _kinds(f)


def test_stall_budget_alert_near_exhaustion():
    lines = [ITER.replace("stall=1/3", "stall=2/3")]
    f = [x for x in analyze(lines, []) if x.kind == HealthKind.STALL_BUDGET]
    assert f and f[0].severity == "alert"


def test_zero_payoff_burn():
    lines = [ITER] + ["  [phase-c/impl] u: U2 test failed (attempt 1)"] * 45 + [ITER]
    f = analyze(lines, [])
    assert HealthKind.ZERO_PAYOFF_BURN in _kinds(f)


def test_attribution_futility_names_units():
    lines = [
        "  [phase-c/impl] src/chain.rs::impl ExactSizeIterator for Chain<'_>: identical errors after attempt 2 codes=— — stopping.",
        "  [phase-c/impl] src/chain.rs::impl ExactSizeIterator for Chain<'_>: identical errors after attempt 2 codes=— — stopping.",
    ]
    f = [x for x in analyze(lines, []) if x.kind == HealthKind.ATTRIBUTION_FUTILITY]
    assert f and "Chain" in f[0].evidence


def test_repeated_heal_alerts():
    lines = ["  [stable] [pre-pass-heal] seeded/resumed tree does not compile; implicated: src/context.rs::with_context"] * 2
    f = [x for x in analyze(lines, []) if x.kind == HealthKind.REPEATED_HEAL]
    assert f and f[0].severity == "alert"


def test_frozen_counter_info():
    lines = ["  [phase-c/per-fn] cluster stable: skipped 6 stub(s) not active in this cluster's cfg-set.",
             "  [phase-c/impl] u: U2 test failed (attempt 1)"]
    f = analyze(lines, [])
    assert HealthKind.FROZEN_COUNTER in _kinds(f)


def test_healthy_segment_is_quiet():
    assert analyze(["  [phase-c/per-fn] 1/5 filled src/lib.rs::f (+10 bytes)"], []) == []
