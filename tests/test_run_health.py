"""Pins for the hourly run-health detectors (pipeline_core/domain/run_health).
Each detector encodes an issue class found once by manual deep analysis and
now detected automatically (user directive 2026-07-10)."""
from __future__ import annotations

from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
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


def _skip_line(n_skipped: int, total: int) -> str:
    return ClusterMembershipReport(
        "stable", total,
        tuple(f"src/nightly.rs::f{i}" for i in range(n_skipped))).log_line()


def test_frozen_counter_info():
    lines = [_skip_line(6, 30), "  [phase-c/impl] u: U2 test failed (attempt 1)"]
    f = [x for x in analyze(lines, []) if x.kind == HealthKind.FROZEN_COUNTER]
    assert f
    # the denominator and the identities must reach the operator: "6 gated"
    # alone reads the same whether 24 stubs are active or none are.
    assert "6 of 30" in f[0].evidence and "24 active" in f[0].evidence
    assert "src/nightly.rs::f0" in f[0].evidence


def test_membership_filter_emptied_alerts():
    """The 9.5h bug: the cfg filter dropped every stub on disk and the counter
    still read as a healthy 'behavioral re-fill' phase."""
    lines = [_skip_line(28, 28), "  [phase-c/impl] u: U2 test failed (attempt 1)"]
    f = [x for x in analyze(lines, [])
         if x.kind == HealthKind.MEMBERSHIP_FILTER_EMPTIED]
    assert f and f[0].severity == "alert"
    assert "0 of 28" in f[0].evidence
    # and it must not simultaneously be labelled a healthy re-fill phase
    assert HealthKind.FROZEN_COUNTER not in _kinds(analyze(lines, []))


def test_partial_skip_is_not_an_emptied_filter():
    assert HealthKind.MEMBERSHIP_FILTER_EMPTIED not in _kinds(
        analyze([_skip_line(6, 30), "  [phase-c/impl] u: U2 test failed (a 1)"], []))


def test_legacy_bare_count_line_yields_no_reading():
    """A denominator-less line cannot say whether the filter is correct, so it
    must produce no finding rather than a guessed one."""
    lines = ["  [phase-c/per-fn] cluster stable: skipped 6 stub(s) not active "
             "in this cluster's cfg-set.",
             "  [phase-c/impl] u: U2 test failed (attempt 1)"]
    assert HealthKind.FROZEN_COUNTER not in _kinds(analyze(lines, []))


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


_SHRUNK = ("  [stable] [phase-c/tdd] iter 1: ORACLE SHRUNK — counting 21 as "
           "failing. 21 named test(s) reported no verdict (binary crashed or "
           "failed to compile): ['test_a']")
# Every expected NAME reported; the total is merely short → an inventory gap,
# not a crash. The localizer is never invoked, so its silence is correct.
_SHRUNK_UNNAMED = (
    "  [stable] [phase-c/tdd] iter 1: ORACLE SHRUNK — counting 1 as failing. "
    "1 unnamed test(s) short of the expected total — every expected NAME "
    "reported, so the static inventory under-enumerated (not a crash)")
_BLAMED = ("  [stable] [phase-c/crash] tests/test_source.rs::test_io_source "
           "SIGSEGV — blamed FnLocator(file='src/error.rs', "
           "fn_name='construct_from_std')")
_DEFERRED = ("  [nightly] [phase-c/crash] 66 test(s) vanished — deferring "
             "localization (>=half the fns they reach are still stubs; stub "
             "panics abort binaries too), fills run first.")


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


def test_unnamed_shortfall_does_not_report_a_broken_localizer():
    """A count-only shrink has no crash to localize; flagging it sends the
    operator after gdb/ASAN wiring that is working fine."""
    f = [x for x in analyze([_SHRUNK_UNNAMED], [])
         if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert f == []


def test_crash_unlocalized_suppressed_when_shrink_was_deferred_as_stub_confounded():
    """Early fill: the vanished tests reach >=half still-stubbed fns, so the
    localizer was deliberately deferred. A stub panic raises no native fault —
    deferral is correct handling, not a broken localizer, and must stay quiet."""
    f = [x for x in analyze([_SHRUNK, _DEFERRED], [])
         if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert f == []


def test_crash_unlocalized_still_fires_on_real_no_localize_despite_a_deferral():
    """A deferral elsewhere must NOT mask a shrink where the localizer actually
    RAN and found nothing (no native fault) — that is still a real concern."""
    no_loc = ("  [stable] [phase-c/crash] 21 test(s) vanished but no crash "
              "could be localized (no native fault under gdb, no ASAN report).")
    (f,) = [x for x in analyze([_SHRUNK, _DEFERRED, _SHRUNK, no_loc], [])
            if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert "deferred as stub-confounded" in f.evidence


_SHRUNK_COMPILE = (
    "  [stable] [phase-c/tdd] iter 2: ORACLE SHRUNK — counting 71 as failing. "
    "66 named test(s) reported no verdict (cluster compile failed — no test "
    "binaries ran): ['test_a']")


def test_crash_unlocalized_ignores_compile_fail_shrinks():
    # No binaries ran, so nothing crashed: a silent localizer is correct.
    f = [x for x in analyze([_SHRUNK_COMPILE], [])
         if x.kind == HealthKind.CRASH_UNLOCALIZED]
    assert f == []


_SHRUNK_UNNAMED_2 = (
    "  [stable] [phase-c/tdd] iter 2: ORACLE SHRUNK — counting 1 as failing. "
    "1 unnamed test(s) short of the expected total — every expected NAME "
    "reported, so the static inventory under-enumerated (not a crash)")


def test_inventory_overcount_alerts_when_shortfall_persists():
    """An unnamed shortfall recurring across iters = the static denominator is
    permanently too big; the cluster can never be declared done. This is the
    class the crash-localizer detector deliberately ignores, so it needs its
    own alert — and a persistent one is structural, not a race."""
    (f,) = [x for x in analyze([_SHRUNK_UNNAMED, _SHRUNK_UNNAMED_2], [])
            if x.kind == HealthKind.INVENTORY_OVERCOUNT]
    assert f.severity == "alert"
    assert "1,2" in f.evidence and "over-count" in f.action


def test_inventory_overcount_warns_on_a_single_iter():
    (f,) = [x for x in analyze([_SHRUNK_UNNAMED], [])
            if x.kind == HealthKind.INVENTORY_OVERCOUNT]
    assert f.severity == "warn"


def test_inventory_overcount_silent_on_named_vanish():
    # A crash (named vanish) is the OTHER detector's job, never this one.
    assert not [x for x in analyze([_SHRUNK], [])
                if x.kind == HealthKind.INVENTORY_OVERCOUNT]


def _u2(unit: str, errs: int) -> str:
    return (f"  [phase-c/impl] {unit}: U2 test failed (attempt 1); "
            f"errs={errs} codes=— floor={errs} stall=0.")


def test_shared_failure_set_across_unrelated_files_alerts():
    """The anyhow plateau's fingerprint: units in 5 unrelated files all rejected
    with the identical errs count — one escalated test set unioned onto every
    producer, so each was convicted for failures it does not own."""
    lines = [_u2("src/context.rs::StdErrorforError", 8),
             _u2("src/error.rs::ErrorImpl<E>", 8),
             _u2("src/kind.rs::Adhoc", 8),
             _u2("src/lib.rs::traitStdError", 8),
             _u2("src/ensure.rs::NotBothDebugfor&(A,B)", 8)]
    f = [x for x in analyze(lines, []) if x.kind == HealthKind.SYSTEMIC_SIGNATURE]
    assert len(f) == 1 and f[0].severity == "alert"
    assert "5 file(s)" in f[0].evidence


def test_same_errs_count_within_one_file_is_not_systemic():
    """Members of one impl family sharing a failing set is ordinary coupling."""
    lines = [_u2("src/error.rs::A", 3), _u2("src/error.rs::B", 3),
             _u2("src/error.rs::C", 3), _u2("src/error.rs::D", 3)]
    assert not [x for x in analyze(lines, [])
                if x.kind == HealthKind.SYSTEMIC_SIGNATURE]


def test_single_failing_test_each_is_not_systemic():
    """errs=1 everywhere is the normal shape of per-fn behavioural work, not a
    shared oracle — it must not drown the real signal."""
    lines = [_u2("src/a.rs::x", 1), _u2("src/b.rs::y", 1),
             _u2("src/c.rs::z", 1), _u2("src/d.rs::w", 1)]
    assert not [x for x in analyze(lines, [])
                if x.kind == HealthKind.SYSTEMIC_SIGNATURE]


# --- cluster build broken -------------------------------------------------
def _shrunk_compile(iter_n: int, digest: str = "") -> str:
    """An ORACLE SHRUNK line as the TDD driver renders a compile-broken sweep:
    the vanished NAMES are replaced by the rustc digest, since every expected
    test vanished for the one build failure."""
    return (f"  [stable] [phase-c/tdd] iter {iter_n}: ORACLE SHRUNK — counting "
            "115 as failing. 100 named test(s) reported no verdict (cluster "
            "compile failed — no test binaries ran); 2 distinct rustc error(s)"
            + (f": {digest}" if digest else
               " — none carried a parseable location"))


def test_cluster_compile_broken_alerts_on_recurrence():
    d = "src/error.rs:12: `NonNull<ErrorImpl>` cannot be sent between threads safely"
    (f,) = [x for x in analyze([_shrunk_compile(1, d), _shrunk_compile(2, d)], [])
            if x.kind == HealthKind.CLUSTER_COMPILE_BROKEN]
    assert f.severity == "alert"
    assert "iter(s) 1,2" in f.evidence
    # The digest must reach the operator: it is the whole point of the detector.
    assert "cannot be sent between threads safely" in f.evidence


def test_cluster_compile_broken_quiet_on_single_transient():
    # One red tree mid-batch is normal; only recurrence proves it structural.
    assert not [x for x in analyze([_shrunk_compile(3)], [])
                if x.kind == HealthKind.CLUSTER_COMPILE_BROKEN]


def test_cluster_compile_broken_survives_unparseable_digest():
    # A link error / ICE carries no `-->` location — still an alert, no digest.
    (f,) = [x for x in analyze([_shrunk_compile(1), _shrunk_compile(2)], [])
            if x.kind == HealthKind.CLUSTER_COMPILE_BROKEN]
    assert "iter(s) 1,2" in f.evidence


def test_compile_broken_is_not_reported_as_a_crash():
    # Same lines must not also fire the crash detector: nothing ran, so a
    # silent localizer is correct behaviour, not a broken one.
    assert not [x for x in analyze([_shrunk_compile(1), _shrunk_compile(2)], [])
                if x.kind == HealthKind.CRASH_UNLOCALIZED]


def test_routing_drop_fires_and_names_the_rejected_headers() -> None:
    # A dropped body is a paid-for LLM answer thrown away; it is invisible in
    # pass/fail because the fn simply stays stubbed.
    lines = [
        "  [phase-c/file] src/wrapper.rs: dropped `fmt` — block header does not "
        "match any impl in this file. raw_block='#[cfg(x)]\\nimpl Debug for "
        "BoxedError' raw_canon='#[cfg(x)]implDebugforBoxedError'",
        "  [phase-c/file] src/wrapper.rs: dropped `source` — block header does not "
        "match any impl in this file. raw_block='#[cfg(x)]\\nimpl StdError for "
        "BoxedError' raw_canon='#[cfg(x)]implStdErrorforBoxedError'",
    ]
    f = [x for x in analyze(lines, []) if x.kind is HealthKind.ROUTING_DROP]
    assert len(f) == 1
    assert f[0].severity == "alert"
    assert "2 returned body(ies)" in f[0].evidence
    assert "Debug for BoxedError" in f[0].evidence


def test_routing_drop_silent_when_nothing_was_dropped() -> None:
    lines = ["  [phase-c/file] src/wrapper.rs: FILE FILLED 4 fn(s) in one pass."]
    assert not [x for x in analyze(lines, []) if x.kind is HealthKind.ROUTING_DROP]
