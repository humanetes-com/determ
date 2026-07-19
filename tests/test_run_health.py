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


def test_zero_payoff_burn_suppressed_when_fills_landed_mid_sweep():
    # I3: pass= is only re-measured per multi-hour sweep, so a flat delta with
    # bodies still landing is mid-sweep progress, not a burning loop. Landed
    # fills must silence the alarm to avoid false-alarm fatigue.
    from pipeline_core.domain.run_health import count_fills_landed
    filled = ["  [phase-c/per-fn] 3/68 filled src/error.rs::root_cause (+40 bytes, body=40)",
              "  [phase-c/impl] impl Display for Error: FILLED 2 fn(s) (attempt 1)."]
    assert count_fills_landed(filled) == 2
    lines = ([ITER] + ["  [phase-c/impl] u: U2 test failed (attempt 1)"] * 45
             + filled + [ITER])
    f = analyze(lines, [])
    assert HealthKind.ZERO_PAYOFF_BURN not in _kinds(f)


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


_RT_FAIL = ("  [layout-retrigger] stable/['ErrorVTable', 'as_ref']: "
            "re-author attempt 1 still did not compile.")
_RT_OK = ("  [layout-retrigger] stable/['ErrorVTable', 'as_ref']: "
          "re-authored successfully (cause=RUNTIME_CRASH)")
_T15_MISSING = ("  [t1.5] stable/('ErrorVTable', 'as_ref'): "
                "missing authored items: as_ref (attempt 2)")


def test_capped_retry_structural_reason_alerts_on_first_occurrence():
    f = [x for x in analyze([_T15_MISSING], [])
         if x.kind == HealthKind.CAPPED_RETRY_FUTILITY]
    assert f and f[0].severity == "alert"
    assert "missing authored items" in f[0].evidence


def test_capped_retry_repeated_compile_fail_alerts():
    f = [x for x in analyze([_RT_FAIL, _RT_FAIL], [])
         if x.kind == HealthKind.CAPPED_RETRY_FUTILITY]
    assert f and "2 re-author attempt(s) failed" in f[0].evidence


def test_capped_retry_single_compile_fail_stays_quiet():
    f = [x for x in analyze([_RT_FAIL], [])
         if x.kind == HealthKind.CAPPED_RETRY_FUTILITY]
    assert f == []


_T15_SCAFFOLD = ("  [t1.5] stable/('ErrorVTable', 'as_ref'): "
                 "authored scaffold did not compile (attempt {}) — reverted.")


def test_capped_retry_repeated_scaffold_compile_fail_alerts():
    # The 2079391 burn: three t1.5 attempts, each "authored scaffold did not
    # compile — reverted", zero alert because only the retrigger wording
    # ("still did not compile") was counted.
    lines = [_T15_SCAFFOLD.format(i) for i in (1, 2, 3)]
    f = [x for x in analyze(lines, [])
         if x.kind == HealthKind.CAPPED_RETRY_FUTILITY]
    assert f and "3 re-author attempt(s) failed" in f[0].evidence


def test_capped_retry_success_suppresses():
    f = [x for x in analyze([_RT_FAIL, _RT_FAIL, _RT_OK], [])
         if x.kind == HealthKind.CAPPED_RETRY_FUTILITY]
    assert f == []


_SHRUNK = ("  [stable] [phase-c/tdd] iter 1: ORACLE SHRUNK — 21 expected "
           "test(s) reported no verdict (binary crashed or failed to compile)")
_BLAMED = ("  [stable] [phase-c/crash] tests/test_source.rs::test_io_source "
           "SIGSEGV — blamed FnLocator(file='src/error.rs', "
           "fn_name='construct_from_std')")


def test_crash_unlocalized_alerts_when_vanish_has_no_blame_line():
    # Vanished tests with no faulting stack = back to blind vanish accounting,
    # the exact state that produced the pass=14 plateau.
    f = [x for x in analyze([_SHRUNK], []) if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert len(f) == 1 and f[0].severity == "alert"
    assert "localizer never logged" in f[0].evidence


def test_crash_unlocalized_reports_explicit_no_localize_lines():
    lines = [_SHRUNK, "  [stable] [phase-c/crash] 21 test(s) vanished but no "
                      "crash could be localized (no native fault under gdb, "
                      "no ASAN report)."]
    (f,) = [x for x in analyze(lines, []) if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert "1 explicit no-localize line(s)" in f.evidence


def test_crash_unlocalized_suppressed_once_a_site_is_blamed():
    # The localizer works; a per-sweep reproduction gap is not a defect.
    lines = [_SHRUNK, _BLAMED, _SHRUNK]
    assert not [x for x in analyze(lines, [])
                if x.kind == HealthKind.CRASH_UNLOCALIZED]


def test_crash_unlocalized_silent_without_vanish():
    assert not [x for x in analyze([ITER], [])
                if x.kind == HealthKind.CRASH_UNLOCALIZED]
