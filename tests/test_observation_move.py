"""A keep that changes what a still-red test says writes that change, and the
owed walk asks the body that produced it first.

The instance, run `19493-36a4e056` (2026-11-04, cluster `stable`, oracle
`2676edb08686`): `behaviour_probe_stable_reach_3e8877f2_0` died with SIGSEGV
inside `src/wrapper.rs::fmt (impl Debug for BoxedError)`, called from line 100
of `tests/behaviour_stable_gen_1.rs`. Fill 3's session wrote that body as a
literal of the nightly twin's expected string; after the keep the stable twin
failed at its assertion on line 100 with the literal's text. The ledger graded
the keep `proven` with the stable twin nowhere in its delta, the owed walk
restarted at tier 0 (`src/error.rs::from_boxed`) under each new fingerprint
(`7a0d807aa101`, `b3eeda405569`, `cda5784ac96d`), and the producing body sat
in tier 4 of 8. Fill 4's session rewrote it for the stable twin, was reverted,
and recorded no ask.

The fixtures below are the recorded test (its source, its file and line), the
recorded owed tiers of iteration 4 (`phase_c.tdd.owed`), and the recorded
ledger premise. The negative control of the move kind is the series shape of
run `1680589-7a6824f8` fill 11 (the assertion moves to a later line of the
test), which must read `later`, never `same_statement`.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.cargo_runner import (
    bind_crash_positions,
    with_crash_positions,
)
from pipeline_core.domain.assertion_position import (
    KeyedVerdicts,
    TestKey,
    crash_positions,
)
from pipeline_core.domain.body_ledger import (
    GATE_VERSION,
    BodyState,
    LandingBefore,
    LedgerEntry,
    LedgerKey,
    ReportKeep,
    SweepView,
    WaveKept,
    classify,
    credited_moves,
    kept_moved_of,
    reseed_allowed,
)
from pipeline_core.domain.candidate_tiers import AskLedger, due_reading
from pipeline_core.domain.crash_site import CrashSite, site_from_frames
from pipeline_core.domain.fill_schedule import RouteKind, RouteLedgerRow, RunOutcome
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.observation_move import MoveKind, move_of
from pipeline_core.domain.premise_advance import advanced_past, observed_by
from pipeline_core.domain.test_results import (
    BinaryResult,
    TestStateReading,
    compare_readings,
)

STABLE = "behaviour_probe_stable_reach_3e8877f2_0"
NIGHTLY = "behaviour_probe_nightly_reach_3e8877f2_0"
FILE = "tests/behaviour_stable_gen_1.rs"
LINE = 100
WRAPPER = FnLocator("src/wrapper.rs", "fmt")

# The recorded test, as the oracle file holds it (lines 93-104).
_SOURCE = "\n" * 92 + """// rule: Formatting an Error created by Error::from_boxed with alternate Debug renders the alternate Debug representation of the original boxed std error.
#[test]
fn behaviour_probe_stable_reach_3e8877f2_0() {
use std::io;
use std::error::Error as StdError;
let boxed: Box<dyn StdError + Send + Sync + 'static> = Box::new(io::Error::new(io::ErrorKind::Other, "from boxed"));
let err = Error::from_boxed(boxed);
    assert_eq!(format!("{:#?}", err), r#"Custom {
    kind: Other,
    error: "from boxed",
}"#);
}
"""


def _source(path: str) -> str | None:
    return _SOURCE if path == FILE else None


def _reading(*, failed: set[str], passed: set[str] = frozenset(),  # type: ignore[assignment]
             aborted: set[str] = frozenset(),  # type: ignore[assignment]
             positions: dict[TestKey, int] | None = None,
             fingerprints: dict[TestKey, str] | None = None,
             crash: dict[TestKey, int] | None = None) -> TestStateReading:
    b = BinaryResult(label=FILE, completed=True, passed=frozenset(passed),
                     failed=frozenset(failed), pass_count=len(passed),
                     fail_count=len(failed), aborted=frozenset(aborted))
    return TestStateReading((b,), positions=positions or {},
                            fingerprints=fingerprints or {},
                            crash_positions=crash or {})


K = TestKey(FILE, STABLE)


# --- the kind -------------------------------------------------------------

def test_a_crash_at_the_line_the_later_reading_asserts_at_is_the_same_statement() -> None:
    assert move_of(before_position=None, before_fingerprint="segv",
                   before_crash=LINE, after_position=LINE,
                   after_fingerprint="msg") is MoveKind.SAME_STATEMENT
    # Same assertion, different text.
    assert move_of(before_position=LINE, before_fingerprint="a",
                   before_crash=None, after_position=LINE,
                   after_fingerprint="b") is MoveKind.SAME_STATEMENT


def test_the_series_shape_reads_later_never_same_statement() -> None:
    """Negative control: run `1680589-7a6824f8` fill 11, the assertion moved
    to a later line of the test's own body."""
    assert move_of(before_position=91, before_fingerprint="a",
                   before_crash=None, after_position=97,
                   after_fingerprint="b") is MoveKind.LATER
    assert move_of(before_position=97, before_fingerprint="a",
                   before_crash=None, after_position=91,
                   after_fingerprint="b") is MoveKind.EARLIER


def test_no_position_on_either_side_is_no_kind() -> None:
    """A panic in crate source or an abort with no localized crash has no
    position: nothing is guessed (the 2026-11-04 review's `unpositioned`)."""
    assert move_of(before_position=None, before_fingerprint="segv",
                   before_crash=None, after_position=LINE,
                   after_fingerprint="msg") is None
    assert move_of(before_position=LINE, before_fingerprint="a",
                   before_crash=None, after_position=LINE,
                   after_fingerprint="a") is None
    # A later assertion than the crash line is not read as anything.
    assert move_of(before_position=None, before_fingerprint="segv",
                   before_crash=LINE - 2, after_position=LINE,
                   after_fingerprint="msg") is None


# --- the comparison every keep reads ---------------------------------------

def test_compare_readings_writes_the_move_from_the_crash_to_the_value() -> None:
    base = _reading(failed={STABLE, NIGHTLY}, aborted={STABLE},
                    fingerprints={K: "segv"}, crash={K: LINE})
    cur = _reading(failed={STABLE}, passed={NIGHTLY},
                   positions={K: LINE}, fingerprints={K: "msg"})
    cmp = compare_readings(base, cur)
    assert cmp.still_failing == {STABLE}
    assert dict(cmp.still_failing_moved) == {STABLE: "same_statement"}
    # Negative control: the same pair with no crash position on the baseline
    # (the pre-fix reading) writes nothing.
    bare = replace(base, crash_positions={})
    assert dict(compare_readings(bare, cur).still_failing_moved) == {}


def test_the_crash_position_is_the_crash_test_frame_in_the_test_own_body() -> None:
    frames = [
        {"file": "src/wrapper.rs", "line": 70, "func": "<anyhow::wrapper::BoxedError as core::fmt::Debug>::fmt"},
        {"file": "/rustc/x/library/core/src/fmt/mod.rs", "line": 1, "func": "core::fmt::write"},
        {"file": FILE, "line": LINE, "func": "behaviour_stable_gen_1::behaviour_probe_stable_reach_3e8877f2_0"},
    ]
    site = site_from_frames(FILE, STABLE, "SIGSEGV", frames)
    assert site.test_line == (FILE, LINE)
    assert crash_positions([site], _source) == {K: LINE}
    # A frame on a line outside the test's own body has no position.
    off = replace(site, test_line=(FILE, 94))
    assert crash_positions([off], _source) == {}
    # A frame in another file than the binary's has none either.
    other = replace(site, test_line=("tests/other.rs", LINE))
    assert crash_positions([other], _source) == {}


def test_a_measurement_takes_the_sweep_crash_position_only_for_the_same_death() -> None:
    reading = _reading(failed={STABLE}, aborted={STABLE},
                       fingerprints={K: "segv"})
    try:
        bind_crash_positions({K: (LINE, "segv")})
        assert dict(with_crash_positions(reading).crash_positions) == {K: LINE}
        bind_crash_positions({K: (LINE, "another death")})
        assert dict(with_crash_positions(reading).crash_positions) == {}
    finally:
        bind_crash_positions({})


# --- the keep channel: route row, wave report, landed row ------------------

def test_kept_since_carries_the_move_and_drops_a_test_a_later_keep_paid() -> None:
    fill_reading.reset_last_comparison()
    mark = fill_reading.kept_mark()
    fill_reading.note_kept(2, newly_green=(NIGHTLY,), still_failing=(STABLE,),
                           moved={STABLE: "same_statement"})
    *_rest, still, moved = fill_reading.kept_since(mark)
    assert still == (STABLE,) and moved == ((STABLE, "same_statement"),)
    fill_reading.note_kept(1, newly_green=(STABLE,), still_failing=(),
                           moved={})
    *_rest, still, moved = fill_reading.kept_since(mark)
    assert still == () and moved == ()
    # A keep site that leaves the move map out records no claim about moves.
    omitted = fill_reading.kept_mark()
    fill_reading.note_kept(0, still_failing=(STABLE,))
    *_rest, still, moved = fill_reading.kept_since(omitted)
    assert still == (STABLE,) and moved is None
    fill_reading.reset_last_comparison()


def test_the_route_row_round_trips_its_moves() -> None:
    row = RouteLedgerRow(kind=RouteKind.SESSION, label="s", targets=(),
                         bodies_sha="", oracle_fp="o", outcome=RunOutcome.CASHED,
                         still_red=(STABLE,), moved=((STABLE, "same_statement"),))
    again = RouteLedgerRow.from_dict(row.to_dict())
    assert again is not None and again.moved == row.moved
    legacy = row.to_dict()
    legacy.pop("moved")
    old = RouteLedgerRow.from_dict(legacy)
    assert old is not None and old.moved is None


def test_a_one_unit_keep_owns_its_moves_whatever_the_attribution() -> None:
    """The session kept `src/wrapper.rs::fmt` alone; the stable twin was not
    attributed to it at that sweep (it sat in tier 4 of its slice)."""
    one = ReportKeep(frozenset({WRAPPER}), frozenset({NIGHTLY}),
                     {STABLE: "same_statement"})
    assert credited_moves([one], {}) == {WRAPPER: {STABLE: "same_statement"}}
    kept = WaveKept({WRAPPER}, [one])
    assert kept_moved_of(kept, WRAPPER, {}) == ((STABLE, "same_statement"),)
    # Two kept units both attributed to the test: credited to neither.
    other = FnLocator("src/error.rs", "from_boxed")
    two = ReportKeep(frozenset({WRAPPER, other}), frozenset(),
                     {STABLE: "same_statement"})
    got = credited_moves([two], {WRAPPER: {STABLE}, other: {STABLE}})
    assert got == {WRAPPER: {}, other: {}}
    # No comparison recorded: None, not "moved nothing".
    assert credited_moves([ReportKeep(frozenset({WRAPPER}), None)], {}) == {WRAPPER: None}


# --- the grade --------------------------------------------------------------

def _row(**kw: object) -> LedgerEntry:
    base = LedgerEntry(
        key=LedgerKey("anyhow", "stable", "src/wrapper.rs", "fmt"),
        body_sha="f04fb2968748d7a0", bodies=("literal",), sig_sha="s",
        layout_fp="l", oracle_fp="2676edb08686",
        convicting=(NIGHTLY, STABLE), kept_green=(NIGHTLY,))
    return replace(base, **kw)  # type: ignore[arg-type]


def _views() -> tuple[SweepView, SweepView]:
    kb = KeyedVerdicts.of_names(failed={STABLE, NIGHTLY}, binary=FILE)
    ka = KeyedVerdicts.of_names(passed={NIGHTLY}, failed={STABLE},
                                positions={K: LINE}, binary=FILE)
    before = SweepView(failed=frozenset({STABLE, NIGHTLY}), verdicts=kb)
    after = SweepView(passed=frozenset({NIGHTLY}), failed=frozenset({STABLE}),
                      positions={K: LINE}, verdicts=ka)
    return before, after


def test_the_grade_copies_the_keep_move_and_the_body_stays_proven() -> None:
    before, after = _views()
    row = _row(kept_moved=((STABLE, "same_statement"),))
    state, delta = classify(row, attributed={NIGHTLY, STABLE}, before=before,
                            after=after, body_now_sha=row.body_sha)
    assert state is BodyState.PROVEN
    assert (NIGHTLY, "gained") in delta and (STABLE, "observed") in delta
    # Negative control, the recorded row: no keep move, the stable twin is
    # nowhere in the delta.
    state0, delta0 = classify(_row(), attributed={NIGHTLY, STABLE},
                              before=before, after=after,
                              body_now_sha=row.body_sha)
    assert state0 is BodyState.PROVEN
    assert all(t != STABLE for t, _k in delta0)


def test_a_move_with_no_gain_is_advanced_not_flat() -> None:
    kb = KeyedVerdicts.of_names(failed={STABLE}, binary=FILE)
    ka = KeyedVerdicts.of_names(failed={STABLE}, positions={K: LINE}, binary=FILE)
    before = SweepView(failed=frozenset({STABLE}), verdicts=kb)
    after = SweepView(failed=frozenset({STABLE}), positions={K: LINE}, verdicts=ka)
    row = _row(convicting=(STABLE,), kept_green=(),
               kept_moved=((STABLE, "same_statement"),))
    state, delta = classify(row, attributed={STABLE}, before=before,
                            after=after, body_now_sha=row.body_sha)
    assert state is BodyState.ADVANCED and delta == ((STABLE, "observed"),)
    state0, _ = classify(replace(row, kept_moved=None), attributed={STABLE},
                         before=before, after=after, body_now_sha=row.body_sha)
    assert state0 is BodyState.FLAT


def test_a_landing_graded_by_the_next_process_reads_its_stored_before() -> None:
    """The cross-process grade: the row carries the sweep it landed on. The
    pre-fix fallback graded it against the after view alone."""
    before, after = _views()
    row = _row(kept_moved=((STABLE, "same_statement"),),
               before=LandingBefore.of_view(before, {NIGHTLY, STABLE}))
    again = LedgerEntry.from_dict(row.to_dict())
    assert again is not None and again.before is not None
    state, delta = classify(again, attributed={NIGHTLY, STABLE},
                            before=again.before.view(), after=after,
                            body_now_sha=row.body_sha)
    assert state is BodyState.PROVEN and (STABLE, "observed") in delta
    # Negative control: the self-grade (before = after) gains nothing.
    state0, delta0 = classify(again, attributed={NIGHTLY, STABLE},
                              before=after, after=after,
                              body_now_sha=row.body_sha)
    assert state0 is not BodyState.PROVEN and (NIGHTLY, "gained") not in delta0


def test_a_bl1_proven_row_is_refused_by_the_gate() -> None:
    row = _row(state=BodyState.PROVEN, gate_ver="bl1")
    got = reseed_allowed(row, file_fp="l", sig_sha="s", oracle_fp="2676edb08686")
    assert not got.allowed and got.reason == f"gate bl1 != {GATE_VERSION}"
    assert GATE_VERSION == "bl2"


# --- the reader and the owed walk ------------------------------------------

def _graded_rows() -> list[LedgerEntry]:
    return [_row(state=BodyState.PROVEN,
                 verdict_delta=((NIGHTLY, "gained"), (STABLE, "observed")))]


def test_the_observed_edge_is_read_from_the_standing_row_and_never_strips_the_unit() -> None:
    rows = _graded_rows()
    key = rows[0].key
    obs = observed_by(rows, oracle_fp="2676edb08686", failing={STABLE},
                      body_now={key: rows[0].body_sha})
    assert obs == {STABLE: frozenset({WRAPPER})}
    assert advanced_past(rows, oracle_fp="2676edb08686", failing={STABLE},
                         body_now={key: rows[0].body_sha}) == {}
    # A body since rewritten has no standing row: the edge is gone with it.
    assert observed_by(rows, oracle_fp="2676edb08686", failing={STABLE},
                       body_now={key: "another"}) == {}


# The recorded `phase_c.tdd.owed` tiers of the stable twin at iteration 4.
_TIERS_ITER4 = [
    ["src/error.rs::from_boxed"],
    ["src/nightly.rs::request_ref", "src/nightly.rs::request_ref_backtrace"],
    ["src/error.rs::deref"],
    ["src/error.rs::construct_from_boxed"],
    ["src/error.rs::fmt", "src/wrapper.rs::fmt"],
    ["src/error.rs::object_ref"],
    ["src/ptr.rs::by_ref", "src/ptr.rs::from_raw"],
    ["src/error.rs::construct", "src/ptr.rs::new"],
]
_EVIDENCE = 'panicked at tests/behaviour_stable_gen_1.rs:100:5:\nerror: "msg"'


def _due(observed: dict[str, frozenset[str]] | None, ledger: AskLedger
         ) -> tuple[str, ...]:
    reach = {u: frozenset(f"f{j}" for j in range(i + 1))
             for i, tier in enumerate(_TIERS_ITER4) for u in tier}
    slice_ = [u for tier in _TIERS_ITER4 for u in tier]
    got = due_reading([STABLE], slices={STABLE: slice_}, reach=reach,
                      evidence={STABLE: _EVIDENCE}, held={}, ledger=ledger,
                      observed=observed)
    return tuple(sorted(got.by_test[STABLE].owed))


def test_the_observed_unit_is_owed_first_and_the_recorded_walk_is_the_control() -> None:
    assert _due(None, AskLedger()) == ("src/error.rs::from_boxed",)
    assert _due({STABLE: frozenset({"src/wrapper.rs::fmt"})}, AskLedger()) == (
        "src/wrapper.rs::fmt",)


def test_three_off_tier_opens_with_the_test_shown_spend_the_rung() -> None:
    """Fill 4's reverted session opened `src/wrapper.rs::fmt` with the stable
    twin in its prompt while the walk owed tier 0. Recorded as a miss, three
    of them spend the rung; the pre-fix accounting (`answered` alone)
    records nothing."""
    obs = {STABLE: frozenset({"src/wrapper.rs::fmt"})}
    ledger = AskLedger()
    reach = {u: frozenset(f"f{j}" for j in range(i + 1))
             for i, tier in enumerate(_TIERS_ITER4) for u in tier}
    slice_ = [u for tier in _TIERS_ITER4 for u in tier]
    reading = due_reading([STABLE], slices={STABLE: slice_}, reach=reach,
                          evidence={STABLE: _EVIDENCE}, held={}, ledger=ledger)
    # Negative control: off the owed tier, `answered` sees nothing.
    asks, misses = reading.answered([WRAPPER], [])
    assert asks == [] and misses == []
    _a, off = reading.off_tier({"src/wrapper.rs::fmt": {"session"}}, [],
                               lambda u, t, r: t == STABLE)
    fp = reading.fingerprints[STABLE]
    assert off == [("src/wrapper.rs::fmt", STABLE, fp, "session")]
    for _ in range(3):
        ledger.miss("src/wrapper.rs::fmt", STABLE, fp, "no_landing")
    # Spent: the observed rung is no longer owed, the walk goes on.
    assert _due(obs, ledger) == ("src/error.rs::from_boxed",)
    # A prompt that did not show the test is not an ask for it.
    _a2, off2 = reading.off_tier({"src/wrapper.rs::fmt": {"unit"}}, [],
                                 lambda u, t, r: False)
    assert off2 == []


def test_an_off_tier_landing_is_an_ask() -> None:
    reading = due_reading([STABLE], slices={STABLE: ["src/error.rs::from_boxed",
                                                     "src/wrapper.rs::fmt"]},
                          reach={"src/error.rs::from_boxed": frozenset({"a"}),
                                 "src/wrapper.rs::fmt": frozenset({"a", "b"})},
                          evidence={STABLE: _EVIDENCE}, held={},
                          ledger=AskLedger())
    asks, misses = reading.off_tier({"src/wrapper.rs::fmt": {"unit"}}, [WRAPPER],
                                    lambda u, t, r: True)
    assert [a[0] for a in asks] == ["src/wrapper.rs::fmt"] and misses == []


def test_crash_site_test_line_defaults_to_none() -> None:
    site = CrashSite(binary_label=FILE, test_name=STABLE, signal="SIGSEGV",
                     frames=())
    assert site.test_line is None and crash_positions([site], _source) == {}
