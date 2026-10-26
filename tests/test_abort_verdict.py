"""The abort partition: one rule for a test a judged write killed without a
panic, read by the session gate, the refill keeps and the ledger grade.

Run `42057-641e589c` (2026-10-26, fills 5 and 6): nine judged session turns
measured net +8 to +20 with nothing newly red, each with an already-red
target dying in a crate body (SIGSEGV, localized), and none was kept: "any
abort refuses whatever was gained". Plan sentence: a strict gain beside the
silent abort of an already-red test whose crash names an authorable crate
frame is kept, with nothing newly red; an abort of a test that was green, or
of a red test whose crash names no crate frame, refuses whatever was
gained. Negative control everywhere: the same reading with no crash sites,
which is the rule before the partition.

Invented shape, no crate names.
"""
from __future__ import annotations

from pipeline_core.domain.abort_verdict import AbortPartition, crash_units_by_test
from pipeline_core.domain.assertion_position import (
    KeyedVerdicts,
    TestKey,
    outcomes_of,
)
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    body_sha,
    classify,
    sig_sha_of,
)
from pipeline_core.domain.crash_site import CrashFrame, CrashSite
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.slice_session import (
    TargetStatus,
    TurnReading,
    judge_against_best,
)

_IN_CRATE = CrashSite("tests/t.rs", "t_red", "SIGSEGV", frames=(
    CrashFrame("src/core.rs", 12, "release", "release"),))
_NO_FRAME = CrashSite("tests/t.rs", "t_red", "SIGSEGV", frames=())


def test_the_sites_name_a_crate_unit_only_through_an_authorable_frame() -> None:
    assert crash_units_by_test([_IN_CRATE]) == {
        "t_red": frozenset({FnLocator("src/core.rs", "release")})}
    assert crash_units_by_test([_NO_FRAME]) == {}


def test_the_partition_refuses_green_and_unlocated_whatever_was_gained() -> None:
    located = AbortPartition.of({"t_red"}, green_before=(), located={"t_red"})
    assert not located.refuses(strict_gain=True, lost=False)
    # The `2157238-210637e6` shape: red before and after, nothing gained.
    assert located.refuses(strict_gain=False, lost=False)
    # A loss beside it.
    assert located.refuses(strict_gain=True, lost=True)
    unlocated = AbortPartition.of({"t_red"}, green_before=(), located=())
    assert unlocated.refuses(strict_gain=True, lost=False)
    was_green = AbortPartition.of({"t_ok"}, green_before={"t_ok"},
                                  located={"t_ok"})
    assert was_green.refuses(strict_gain=True, lost=False)


def _readings() -> tuple[KeyedVerdicts, KeyedVerdicts]:
    before = KeyedVerdicts.of_names(passed=("t_up",), failed=("t_red",),
                                    binary="tests/t.rs")
    after = KeyedVerdicts.of_names(passed=("t_up",), failed=("t_red",),
                                   aborted_silent=("t_red",),
                                   binary="tests/t.rs")
    return before, after


def test_outcomes_locate_an_abort_only_from_the_sites_handed_in() -> None:
    before, after = _readings()
    got = outcomes_of({"t_red"}, before=before, after=after,
                      crash_sites=(_IN_CRATE,))
    assert got.aborted == {"t_red"} and got.aborted_located == {"t_red"}
    assert not got.aborted_green
    # Negative control: no sites, the abort reads unlocated and refuses.
    bare = outcomes_of({"t_red"}, before=before, after=after)
    assert bare.aborted == {"t_red"} and not bare.aborted_located
    assert bare.refuses_with(strict_gain=True)
    assert not got.refuses_with(strict_gain=True)


def _turn(delta: int, **kw: object) -> TurnReading:
    return TurnReading(targets=(TargetStatus("t_red", False, "aborted"),),
                       pass_delta=delta, **kw)  # type: ignore[arg-type]


def test_a_turn_with_a_strict_gain_and_a_located_red_abort_is_kept() -> None:
    kept = _turn(20, aborted=("t_red",), aborted_located=("t_red",))
    assert not kept.regressed and kept.refused_aborts == ()
    # Controls: the same turn unlocated (the rule before), at net 0, and
    # with a test newly red beside it.
    assert _turn(20, aborted=("t_red",)).regressed
    assert _turn(0, aborted=("t_red",), aborted_located=("t_red",)).regressed
    assert _turn(20, aborted=("t_red",), aborted_located=("t_red",),
                 newly_red=("t_other",)).regressed


def test_the_best_state_is_the_gain_not_the_turn_after_it() -> None:
    # Fill 5's shape: turn 6 +20 with the located abort, turn 7 +0 with the
    # target failing normally. Turn 7 is worse than turn 6.
    six = _turn(20, aborted=("t_red",), aborted_located=("t_red",))
    seven = _turn(0)
    assert not judge_against_best(seven, six).holds
    # And turn 6 against an earlier +14 turn without the abort holds.
    assert judge_against_best(six, _turn(14)).holds
    # Negative control: unlocated, turn 6 does not hold against turn 5.
    assert not judge_against_best(_turn(20, aborted=("t_red",)),
                                  _turn(14)).holds


_KEY = LedgerKey(project="p", cluster="c", file="src/core.rs", fn_name="make")


def _landed(**kw: object) -> LedgerEntry:
    base = {"key": _KEY, "body_sha": body_sha(("x",)), "bodies": ("x",),
            "sig_sha": sig_sha_of(("fn make",)), "layout_fp": "fp",
            "state": BodyState.LANDED, "oracle_fp": "o"}
    base.update(kw)
    return LedgerEntry(**base)  # type: ignore[arg-type]


def _sweep(*, after_sites: tuple[CrashSite, ...]) -> tuple[SweepView, SweepView]:
    before = SweepView.of_sweep(
        passed={"t_up"}, failed={"t_red"}, vanished=(), intact=True, epoch=0,
        positions={}, aborted=(), aborted_silent=(),
        verdicts=KeyedVerdicts.of_names(passed=("t_up",), failed=("t_red",),
                                        binary="tests/t.rs"))
    after = SweepView.of_sweep(
        passed={"t_up"}, failed={"t_red"}, vanished=(), intact=True, epoch=0,
        positions={}, aborted=("t_red",), aborted_silent=("t_red",),
        verdicts=KeyedVerdicts.of_names(
            passed=("t_up",), failed=("t_red",), aborted=("t_red",),
            aborted_silent=("t_red",), binary="tests/t.rs"),
        crash_sites=after_sites)
    return before, after


def test_the_ledger_does_not_refute_a_paying_keep_on_a_located_red_abort() -> None:
    entry = _landed(kept_green=("t_up_elsewhere",))
    before, after = _sweep(after_sites=(_IN_CRATE,))
    state, delta = classify(entry, attributed={"t_red"}, before=before,
                            after=after, body_now_sha=entry.body_sha)
    assert state is not BodyState.REFUTED
    assert ("t_red", "aborted") in delta
    # Negative control: the sweep's sites not handed in.
    before, bare = _sweep(after_sites=())
    state, _ = classify(entry, attributed={"t_red"}, before=before,
                        after=bare, body_now_sha=entry.body_sha)
    assert state is BodyState.REFUTED
    # And a keep that gained nothing is refuted even with the site.
    nothing = _landed(kept_green=())
    before, after = _sweep(after_sites=(_IN_CRATE,))
    state, _ = classify(nothing, attributed={"t_red"}, before=before,
                        after=after, body_now_sha=nothing.body_sha)
    assert state is BodyState.REFUTED


def test_judge_refill_reads_the_gates_crash_sites() -> None:
    from pipeline_core.domain.test_report import U2Verdict, judge_refill
    from pipeline_core.domain.test_results import (
        BinaryResult,
        TestStateReading,
    )
    baseline = TestStateReading(binaries=(BinaryResult(
        label="tests/t.rs", completed=True, passed=frozenset(),
        failed=frozenset({"t_red", "t_new"}), pass_count=0, fail_count=2),))

    def _verdict(sites: tuple[CrashSite, ...]) -> U2Verdict:
        return U2Verdict.reject(
            frozenset({"t_red"}), "aborted", aborted=frozenset({"t_red"}),
            self_passed=frozenset({"t_new"}), crash_sites=sites)

    kept = judge_refill({"t_red", "t_new"}, baseline, _verdict((_IN_CRATE,)))
    assert kept.outcomes.gained == {"t_new"}
    assert kept.non_regression and kept.banks
    refused = judge_refill({"t_red", "t_new"}, baseline, _verdict(()))
    assert not refused.non_regression and "aborted: t_red" in refused.refusal


def test_a_test_key_is_judged_per_binary() -> None:
    # The partition reads bare names of the keys it was handed; a site in
    # another binary of the same name still locates it (bare-name join, as
    # the sight reads it).
    before = KeyedVerdicts(failed=frozenset({TestKey("tests/a.rs", "t_red")}))
    after = KeyedVerdicts(failed=frozenset({TestKey("tests/a.rs", "t_red")}),
                          aborted_silent=frozenset({TestKey("tests/a.rs",
                                                            "t_red")}))
    got = outcomes_of({"t_red"}, before=before, after=after,
                      crash_sites=(_IN_CRATE,))
    assert got.aborted_located == {"t_red"}


def test_a_turn_refusing_an_abort_the_best_forgave_does_not_hold() -> None:
    """Review of 2026-10-26: the best state's aborts were subtracted whether
    or not its gain forgave them, so a later turn that refuses the same test
    held and the next turn was written on it. Negative control: the
    pre-change subtraction (every aborted target of the best) holds."""
    from dataclasses import replace

    from pipeline_core.domain.assertion_position import TestOutcomes
    best = _turn(20, aborted=("t_red",), aborted_located=("t_red",))
    worse = _turn(20, aborted=("t_red",))          # unlocated: refused
    assert worse.refused_aborts == ("t_red",) and best.refused_aborts == ()
    assert not judge_against_best(worse, best).holds
    old = TestOutcomes(aborted=frozenset(worse.refused_aborts)
                       - frozenset(best.aborted))
    assert not old.aborted, "pre-change: the refused abort vanished"
    assert replace(best, pass_delta=20).refused_aborts == ()


def test_a_clean_turn_still_prints_the_crash_it_saw() -> None:
    from pipeline_core.domain.slice_session import render_test_turn
    clean = TurnReading(targets=(TargetStatus("t_a", True),),
                        crash="#0 release at src/core.rs:12")
    text = render_test_turn(clean)
    assert "KEPT" in text and "src/core.rs:12" in text
    assert "src/core.rs" not in render_test_turn(
        TurnReading(targets=(TargetStatus("t_a", True),)))


def test_the_pair_last_error_quotes_the_stored_crash() -> None:
    """Review of 2026-10-26: the pair stored a 240-character preview of a
    revert's detail, and the crash a gate appends was what that cut first;
    a U2 revert stored nothing. The record keeps the crash whole and the
    next pair prompt quotes it. Negative control: without it, nothing of
    the site reaches LAST_ERROR."""
    from pipeline_core.domain.last_error import LastErrorKind
    from pipeline_core.domain.pair_fill import pair_last_error
    crash = "Faulting access:\n  #0 release at src/core.rs:12"
    got = pair_last_error("", "binary crashed", "fn body() {}",
                          kind=LastErrorKind.CRASHED, crash=crash)
    assert "src/core.rs:12" in got.text and got.kind is LastErrorKind.CRASHED
    assert got.text.index("src/core.rs:12") < got.text.index("fn body()")
    bare = pair_last_error("", "binary crashed", "fn body() {}",
                           kind=LastErrorKind.CRASHED)
    assert "src/core.rs" not in bare.text
