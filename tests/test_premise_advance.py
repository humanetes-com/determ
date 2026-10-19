"""A unit whose own keep moved a still-red test's failure later is not that
test's owner at the new failure.

The recorded instance, run `1680589-7a6824f8` (cluster `stable`): fill 11
rewrote the error's debug formatter, three tests turned green and three more
logged `panic sites moved — 3 test(s) panic LATER` (`tests/error_gen_3.rs`
41->60, `tests/error_gen_7.rs` 51->52, `tests/test_fmt.rs` 90->91). The
ledger graded the landing `proven` with only the three gains, and the next
sweep still owned the movers by the formatter just fixed; fill 12 rewrote it
twice for nothing. The positions and test names below are that log line's;
the sweep captures themselves are not stored.
"""
from __future__ import annotations

from pipeline_core.domain.assertion_position import TestKey
from pipeline_core.domain.attribution import AttributionResult, FailureKind, FormatterPair
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    body_sha,
    classify,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.premise_advance import (
    advanced_past,
    owning_view,
    stranded,
    without_advanced,
)
from pipeline_core.domain.slice_session import session_slices_for_failing

_FMT = FnLocator("src/fmt.rs", "debug")
_DISP = FnLocator("src/error.rs", "fmt")
_GAINED = ("alt_debug_after_boxed_roundtrip_exercises_error_impl_debug",
           "test_display_error_debug_via_option_context",
           "wrapper_message_error_debug_alternate")
_MOVED = {"error_alt_debug_exercises_error_impl_debug": ("tests/error_gen_3.rs", 41, 60),
          "alt_debug_context_chain_exercises_alternate_path": ("tests/error_gen_7.rs", 51, 52),
          "test_altdebug": ("tests/test_fmt.rs", 90, 91)}
_STILL = "alt_debug_nested_error_new_hits_error_impl_debug"
_ORACLE = "oracle-1a81b6"


def _row(state: BodyState = BodyState.LANDED, delta: tuple[tuple[str, str], ...] = (),
         *, oracle: str = _ORACLE, body: str = "f45c6980") -> LedgerEntry:
    return LedgerEntry(
        key=LedgerKey("anyhow", "stable", _FMT.file, _FMT.fn_name),
        body_sha=body_sha((body,)), bodies=(body,), sig_sha="s", layout_fp="l",
        state=state, verdict_delta=delta, oracle_fp=oracle,
        kept_green=_GAINED)


def _fill_11(moved: bool) -> tuple[SweepView, SweepView]:
    red = frozenset({*_GAINED, *_MOVED, _STILL})
    # Keyed as the view keys its verdicts (no binary on a name-set view):
    # a position compares only under the same key as its verdict.
    before = SweepView(failed=red, positions={
        TestKey("", t): b for t, (_f, b, _a) in _MOVED.items()})
    after = SweepView(passed=frozenset(_GAINED), failed=red - set(_GAINED), positions={
        TestKey("", t): (a if moved else b) for t, (_f, b, a) in _MOVED.items()})
    return before, after


def test_a_landing_that_pays_some_tests_still_records_the_ones_it_moved() -> None:
    """Negative control: the same landing with no position moved records no
    advance; the gains alone grade it proven either way."""
    e = _row()
    state, delta = classify(e, attributed={*_GAINED, *_MOVED, _STILL},
                            before=_fill_11(True)[0], after=_fill_11(True)[1],
                            body_now_sha=e.body_sha)
    assert state is BodyState.PROVEN
    assert {t for t, k in delta if k == "advanced"} == set(_MOVED)
    assert {t for t, k in delta if k == "gained"} == set(_GAINED)
    state0, delta0 = classify(e, attributed={*_GAINED, *_MOVED, _STILL},
                              before=_fill_11(False)[0], after=_fill_11(False)[1],
                              body_now_sha=e.body_sha)
    assert state0 is BodyState.PROVEN and not [k for _t, k in delta0 if k == "advanced"]


def test_the_advanced_tests_are_read_off_the_standing_row_under_this_oracle() -> None:
    graded = _row(BodyState.PROVEN, tuple((t, "advanced") for t in _MOVED))
    failing = {*_MOVED, _STILL}
    adv = advanced_past([_row(), graded], oracle_fp=_ORACLE, failing=failing)
    assert adv == {t: frozenset({_FMT}) for t in _MOVED}
    # Another oracle's grade says nothing about this tree; a test that turned
    # green is nobody's to re-own; a later row about a new body supersedes.
    assert advanced_past([graded], oracle_fp="oracle-other", failing=failing) == {}
    assert advanced_past([graded], oracle_fp=_ORACLE, failing={_STILL}) == {}
    # A rewritten body: the on-tree view reads the row about the body on
    # disk; the re-seed view (`latest_by_unit`) would keep the old proven one.
    rewritten = _row(body="new")
    now = {rewritten.key: rewritten.body_sha}
    assert advanced_past([graded, rewritten], oracle_fp=_ORACLE, failing=failing,
                         body_now=now) == {}
    assert advanced_past([graded, rewritten], oracle_fp=_ORACLE, failing=failing)


def test_the_movers_leave_the_formatter_and_the_unmoved_test_stays() -> None:
    """The iter-12 shape: the formatter and its dispatcher (a hop pair) own
    the four leftovers. After the strip the formatter owns only the test it
    did not move; the dispatcher keeps all four, and the pair stays because
    a member still owns a test. Negative control: no advance, no change."""
    tests = {*_MOVED, _STILL}
    attr = AttributionResult(
        frontier={_FMT: set(tests), _DISP: set(tests)},
        kind=FailureKind.BEHAVIOR,
        convicting={FnSite(_FMT.file, _FMT.fn_name, "impl ErrorImpl"): set(tests)},
        pairs=frozenset({FormatterPair(FnSite(_DISP.file, "fmt", "impl Debug for Error"),
                                       FnSite(_FMT.file, "debug", "impl ErrorImpl"))}))
    adv = {t: frozenset({_FMT}) for t in _MOVED}
    out = without_advanced(attr, adv)
    assert out.frontier[_FMT] == {_STILL} and out.frontier[_DISP] == tests
    assert list(out.convicting.values()) == [{_STILL}]
    assert out.pairs == attr.pairs
    assert without_advanced(attr, {}) is attr
    solo = without_advanced(attr._replace(frontier={_FMT: set(_MOVED)}), adv)
    assert solo.frontier == {} and solo.pairs == frozenset()


def test_a_test_nothing_else_owns_keeps_its_advanced_past_unit() -> None:
    adv = {t: frozenset({_FMT}) for t in _MOVED}
    owned = {"test_altdebug": {FnLocator("src/context.rs", "fmt")}}
    back = stranded(adv, owned)
    assert set(back) == set(_MOVED) - {"test_altdebug"}


def test_the_moved_tests_session_slices_drop_the_formatter_after_the_pair_hop() -> None:
    """The owed-slice consumer the first cut missed: the session slice of a
    moved test is the coverage owners of that test (which still list the
    formatter, since the test enters it) plus the hop from its dispatcher.
    Taken through the ownership view the formatter leaves the movers' slices
    and stays on the unmoved test's. Negative control: the raw slices, as
    shipped first, hold the formatter for every mover."""
    fmt = FnSite(_FMT.file, "debug", "impl ErrorImpl")
    disp = FnSite(_DISP.file, "fmt", "impl Debug for Error")
    ctx = FnSite("src/context.rs", "fmt", "impl Debug for ContextError < C , E >")
    tests = [*_MOVED, _STILL]
    cov = {t: {fmt, ctx} for t in tests}
    raw = session_slices_for_failing(tests, owners=cov,
                                     units_by_test={t: {disp} for t in tests},
                                     pairs=[(disp, fmt)])
    assert all(fmt in raw[t] for t in tests)
    adv = {t: frozenset({_FMT}) for t in _MOVED}
    owned = owning_view(raw, adv, owned_elsewhere=tests)
    assert all(fmt not in owned[t] and ctx in owned[t] for t in _MOVED)
    assert fmt in owned[_STILL]


def test_the_view_keeps_a_test_its_only_owner_and_keeps_every_key() -> None:
    adv = {"test_altdebug": frozenset({_FMT})}
    fmt = FnSite(_FMT.file, "debug", "impl ErrorImpl")
    assert owning_view({"test_altdebug": {fmt}}, adv) == {"test_altdebug": frozenset({fmt})}
    assert owning_view({"test_altdebug": {fmt}}, adv, owned_elsewhere={"test_altdebug"}) == {
        "test_altdebug": frozenset()}
