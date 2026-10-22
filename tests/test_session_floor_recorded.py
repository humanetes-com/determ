"""A session's floor prices its first turn apart, and a share cut is owed once
per test.

The defect, run `450922-661c5e9f` (2026-10-05, cluster `stable`, oracle
`21fc1d6d63df`): the fill reserved each opened slice session three turns at
the per-call median of the ledger's session rows — 3 × 3.98 = 11.9 min at
fill 9, 3 × 4.19 = 12.6 at fill 10 — while a session's first turn (baseline
suite, turn-0 prompt, its deliberation) ran 13–33 min in the tail and the
turns after it mostly under 5. Six of those seven sessions ran one turn and
were cut by the share. A cut row ranks `served` (the share ended it, not the
route), so the same tests were opened and cut again, re-packed every fill
into a slice no exact-set rule recognised: 9 cut rows, 234 of 493 session
minutes, net 0.

The fixture (`scripts/session_floor_fixture.py`) is fills 9–12 of that run:
each fill's planned runs from its `phase_c.fill.schedule` event and the
route ledger cut at that fill's first own row, with this run's session rows'
`first_turn_minutes` backfilled from their `phase_c.session.turn` events.
The negative control of the price half is the pre-change rule, recomputed
here: the uniform per-call median over the same window. The bound's control
is the 450977 row, a single cut that stays `served`
(`tests/test_fill_schedule_unit_service.py`). A replay asserts reachability
and rank, never a score.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.fill_schedule import (
    FillSchedule,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    ScheduledRun,
    SessionTurnPrice,
    schedule,
    session_turn_price,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "450922-661c5e9f_session_floor.json")
RUN = "450922-661c5e9f"
ALT_CONTAINS = "display_error_debug_alt_contains_display_only_context"
IS_REACHED = "display_error_debug_is_reached_for_two_levels"
ALTDEBUG = "test_altdebug"
_DIVISIBLE = (RouteKind.FILE, RouteKind.IMPL, RouteKind.PER_FN)


def _loc(s: str) -> FnLocator:
    f, _sep, fn = s.partition("::")
    return FnLocator(f, fn)


def _fixture() -> dict[str, Any]:
    fix: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return fix


def _fill(fix: Mapping[str, Any], it: int) -> dict[str, Any]:
    return next(f for f in fix["fills"] if f["iter"] == it)


def _runs(fill: Mapping[str, Any]) -> list[RouteRun]:
    out = []
    for r in fill["planned"]:
        kind = RouteKind(r["kind"])
        targets = sorted((_loc(t) for t in r["targets"]),
                         key=lambda u: (u.file, u.fn_name))
        tests = frozenset(r.get("target_tests") or ())
        out.append(RouteRun(
            kind=kind, label=r["label"], targets=frozenset(targets),
            target_tests=tests,
            stubs_in_targets=frozenset(_loc(t) for t in r.get("stubs") or ()),
            files=tuple(r.get("files") or ()), seq=int(r.get("seq") or 0),
            bodies_sha=str(r.get("bodies_sha") or ""),
            unit_tests=(tuple((t, tests) for t in targets)
                        if kind in _DIVISIBLE else ())))
    return out


def _rows(fix: Mapping[str, Any], fill: Mapping[str, Any]) -> list[RouteLedgerRow]:
    rows = [RouteLedgerRow.from_dict(r) for r in fix["rows"][:fill["ledger_cut"]]]
    return [r for r in rows if r is not None]


def _replay(fix: Mapping[str, Any], it: int) -> FillSchedule:
    fill = _fill(fix, it)
    inp = fill["inputs"]
    return schedule(
        _runs(fill), _rows(fix, fill), oracle_fp=fix["oracle_fp"], hollow={},
        budget_minutes=fill["budget_minutes"],
        readmit_after=int(inp.get("readmit_after", 2)),
        max_per_kind={RouteKind(k): int(v)
                      for k, v in (inp.get("max_per_kind") or {}).items()},
        attributed=[_loc(x) for x in inp.get("attributed") or ()],
        session_min_turns=int(inp.get("session_min_turns", 3)))


def _sessions(s: FillSchedule) -> list[tuple[ScheduledRun, bool]]:
    return ([(r, True) for r in s.ordered if r.run.kind is RouteKind.SESSION]
            + [(r, False) for r in s.deferred if r.run.kind is RouteKind.SESSION])


def _aimed_only(s: FillSchedule, test: str) -> tuple[ScheduledRun, bool]:
    return next((r, opened) for r, opened in _sessions(s)
                if r.run.target_tests == frozenset({test}))


def _pre_change_price(rows: Iterable[RouteLedgerRow], run: RouteRun) -> float:
    """The rule this replaces, for the negative control: the median minutes
    per call over the session's work-key rows, else the newest 20 session
    rows."""
    def key(kind: RouteKind, targets: Iterable[tuple[str, str]] | Iterable[FnLocator],
            tests: Iterable[str]) -> tuple[Any, ...]:
        names = sorted(f"{t[0]}::{t[1]}" if isinstance(t, tuple)
                       else f"{t.file}::{t.fn_name}" for t in targets)
        return (kind, tuple(names), tuple(sorted(tests)))

    sessions = [r for r in rows if r.kind is RouteKind.SESSION
                and r.model_calls > 0 and r.minutes > 0]
    k = key(run.kind, run.targets, run.target_tests)
    own = [r for r in sessions if key(r.kind, r.targets, r.target_tests) == k]
    per = sorted(r.minutes / r.model_calls for r in (own or sessions)[-20:])
    mid = len(per) // 2
    return per[mid] if len(per) % 2 else (per[mid - 1] + per[mid]) / 2


def _own_row(fix: Mapping[str, Any], it: int, label: str) -> dict[str, Any]:
    return next(r for r in fix["rows"]
                if r.get("run_id") == RUN and r.get("iteration") == it
                and r.get("label") == label and r.get("outcome") != "deferred")


# --- recorded fills ---------------------------------------------------------

def test_fills_9_and_10_reserve_a_first_turn_the_old_price_cut_short() -> None:
    """Every session fills 9 and 10 opened was reserved 11.7 and 13.3 min
    live. Its own row shows the first turn it then ran. Under the two-part
    price the floor is the first-turn median plus two later turns, never
    the uniform per-call reading, and more of those first turns fit inside
    the share with room left for a second. Control: the pre-change price
    recomputed on the same rows is the live floor."""
    fix = _fixture()
    fits_new = fits_old = opened_n = 0
    for it, live_turn in ((9, 3.9), (10, 4.2)):
        fill = _fill(fix, it)
        rows = _rows(fix, fill)
        for sr, opened in _sessions(_replay(fix, it)):
            if not opened:
                continue
            opened_n += 1
            old = _pre_change_price(rows, sr.run)
            assert round(old, 1) == live_turn
            price = session_turn_price(rows, sr.run)
            assert price is not None
            assert sr.floor_minutes is not None
            assert abs(sr.floor_minutes - price.floor(3)) < 1e-6
            assert sr.floor_minutes > 3 * old
            first = _own_row(fix, it, sr.run.label)["first_turn_minutes"]
            fits_new += first < (sr.share_minutes or 0.0)
            fits_old += first < 3 * old
    assert opened_n == 8
    # Live, one session's first turn fitted its share (the 9.8-min turn of
    # the iteration-9 cash); the replay fits more than that.
    assert fits_old == 1
    assert fits_new > fits_old


def test_fill_10_alt_contains_is_priced_from_its_own_first_turn() -> None:
    """The session on `ALT_CONTAINS` was cut at fill 9 after one 20.4-min
    turn. Its own first turn is the price of the next one: the fill-10
    floor holds that first turn and two later ones, where the live 13.3-min
    share could not hold the 16.1-min first turn fill 10 then ran."""
    fix = _fixture()
    sr, opened = _aimed_only(_replay(fix, 10), ALT_CONTAINS)
    assert opened and sr.reason == "served"
    price = session_turn_price(_rows(fix, _fill(fix, 10)), sr.run)
    assert price is not None and abs(price.first - 20.36) < 0.01
    first_run = _own_row(fix, 10, sr.run.label)["first_turn_minutes"]
    assert sr.share_minutes is not None and sr.share_minutes > first_run
    live = next(r for r in _fill(fix, 10)["recorded"] if r["label"] == sr.run.label)
    assert live["share_minutes"] < first_run


def test_fill_11_defers_alt_contains_after_two_cuts_in_a_row() -> None:
    """Cut at fills 9 and 10, one call each, both on that test alone. The
    exemption was owed once: fill 11 grades the fill-10 row (FLAT, kept
    nothing) as the flat re-buy and defers it. Live it opened at 48.3 min
    and spent 51.4 min and 7 calls on NO_CHANGE."""
    fix = _fixture()
    sr, opened = _aimed_only(_replay(fix, 11), ALT_CONTAINS)
    assert not opened and sr.reason == "rebuy_flat"
    live = next(r for r in _fill(fix, 11)["recorded"] if r["label"] == sr.run.label)
    assert live["reason"] == "served" and live["share_minutes"] > 48


def test_fill_12_defers_altdebug_cut_twice_under_two_packings() -> None:
    """`test_altdebug` was cut at fill 10 with `test_downcast_low` and at fill
    11 with `test_downcast_mid`: no two slices on it aimed at the same set,
    so a rule keyed on the exact set never fires. Keyed per test, fill 12's
    slice on it alone is the re-buy of the fill-11 row. Live it opened at
    30.75 min."""
    fix = _fixture()
    sr, opened = _aimed_only(_replay(fix, 12), ALTDEBUG)
    assert not opened and sr.reason == "rebuy_flat"
    live = next(r for r in _fill(fix, 12)["recorded"] if r["label"] == sr.run.label)
    assert live["reason"] == "served"


def test_fill_12_stays_a_rebuy_when_the_slice_adds_an_unopened_unit() -> None:
    """The unit path ranks a slice `never_served` when one target has no
    opened row, ahead of the test-keyed re-buy. On fill 12 each deferred
    session's units were already opened; adding one that no route has
    opened keeps the re-buy. A test no session has tried, added to the
    same slice, stays a new question."""
    import dataclasses

    from pipeline_core.domain.models import FnLocator

    fix = _fixture()
    fill = _fill(fix, 12)
    rows = _rows(fix, fill)
    inp = fill["inputs"]
    fresh = FnLocator("src/not_opened.rs", "never_touched")
    base = _runs(fill)
    for test in (ALT_CONTAINS, IS_REACHED, ALTDEBUG):
        runs = []
        for run in base:
            if run.kind is RouteKind.SESSION and run.target_tests == frozenset({test}):
                run = dataclasses.replace(
                    run, targets=frozenset(set(run.targets) | {fresh}),
                    bodies_sha=(run.bodies_sha or "") + "x")
            runs.append(run)
        sched = schedule(
            runs, rows, oracle_fp=fix["oracle_fp"], hollow={},
            budget_minutes=fill["budget_minutes"],
            readmit_after=int(inp.get("readmit_after", 2)),
            max_per_kind={RouteKind(k): int(v)
                          for k, v in (inp.get("max_per_kind") or {}).items()},
            attributed=[_loc(x) for x in inp.get("attributed") or ()],
            session_min_turns=int(inp.get("session_min_turns", 3)))
        hit = next(sr for sr in (*sched.ordered, *sched.deferred)
                   if fresh in sr.run.targets)
        assert hit in sched.deferred and hit.reason == "rebuy_flat", test
    # The same unopened unit on a test no session has tried is the new
    # question the unit path names, not a re-buy of the tests beside it.
    alone = next(r for r in base
                 if r.kind is RouteKind.SESSION
                 and r.target_tests == frozenset({ALT_CONTAINS}))
    added = dataclasses.replace(
        alone, targets=frozenset(set(alone.targets) | {fresh}),
        target_tests=frozenset({ALT_CONTAINS, "test_no_session_has_tried"}),
        bodies_sha=(alone.bodies_sha or "") + "x")
    sched = schedule(
        [added if r is alone else r for r in base], rows,
        oracle_fp=fix["oracle_fp"], hollow={},
        budget_minutes=fill["budget_minutes"],
        readmit_after=int(inp.get("readmit_after", 2)),
        max_per_kind={RouteKind(k): int(v)
                      for k, v in (inp.get("max_per_kind") or {}).items()},
        attributed=[_loc(x) for x in inp.get("attributed") or ()],
        session_min_turns=int(inp.get("session_min_turns", 3)))
    hit = next(sr for sr in (*sched.ordered, *sched.deferred)
               if fresh in sr.run.targets)
    assert hit in sched.ordered and hit.reason == "never_served"


def test_fill_12_still_opens_the_sessions_whose_tests_have_paid() -> None:
    """The behaviour-probe session cashed at fill 10 and is opened at fill 12
    with a floor; the bound does not convict a test a session paid."""
    fix = _fixture()
    s = _replay(fix, 12)
    opened = [sr for sr, o in _sessions(s) if o]
    assert any("behaviour_probe_stable_context_then_into_boxed_2" in sr.run.target_tests
               for sr in opened)
    assert all(sr.floor_minutes for sr in opened)


# --- the price --------------------------------------------------------------

def _srow(tests: tuple[str, ...], *, minutes: float, calls: int,
          first: float | None = None, outcome: RunOutcome = RunOutcome.FLAT,
          share: float | None = None, targets: tuple[str, ...] = ("a",),
          started: str = "", green: tuple[str, ...] | None = None,
          kind: RouteKind = RouteKind.SESSION, delta: int = 0) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label=f"{kind.value} {'+'.join(tests)}", bodies_sha="b",
        targets=tuple(("src/x.rs", t) for t in targets), oracle_fp="o",
        outcome=outcome, minutes=minutes, model_calls=calls,
        target_tests=tests, share_minutes=share, started=started,
        first_turn_minutes=first, newly_green=green, net_pass_delta=delta)


def _session(tests: Iterable[str], targets: Iterable[str] = ("a",)) -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label="session", bodies_sha="new",
                    targets=frozenset(FnLocator("src/x.rs", t) for t in targets),
                    target_tests=frozenset(tests))


def test_the_price_splits_first_and_later_turns() -> None:
    rows = [_srow(("t",), minutes=24.0, calls=5, first=16.0),
            _srow(("t",), minutes=20.0, calls=3, first=14.0)]
    price = session_turn_price(rows, _session(("t",)))
    assert price == SessionTurnPrice(first=15.0, later=2.5)
    assert price.floor(3) == 20.0
    assert price.note(3) == "floor 15.0m+2x2.5m"
    # Control: the same rows under the per-call rule reserve 3 x 5.8 = 17.4,
    # which a 16-min first turn and one later turn already overrun.
    assert abs(_pre_change_price(rows, _session(("t",))) - (24 / 5 + 20 / 3) / 2) < 1e-9


def test_a_one_call_row_is_a_first_turn_sample_without_the_field() -> None:
    """Rows written before the field: a one-call row is its first turn whole;
    a multi-call row gives only an upper bound on the later turns."""
    rows = [_srow(("t",), minutes=20.0, calls=1),
            _srow(("t",), minutes=30.0, calls=6)]
    assert session_turn_price(rows, _session(("t",))) == SessionTurnPrice(20.0, 5.0)


def test_own_rows_are_those_aimed_at_a_shared_test_whatever_the_units() -> None:
    """A re-packed slice keeps its tests' own price: the unit set is not part
    of the key. Control: a row aimed elsewhere is only the window."""
    mine = _srow(("t", "u"), minutes=30.0, calls=1, targets=("a", "b"))
    other = _srow(("z",), minutes=4.0, calls=1, targets=("c",))
    price = session_turn_price([mine, other], _session(("t",), targets=("d",)))
    assert price is not None and price.first == 30.0
    elsewhere = session_turn_price([mine, other], _session(("q",), targets=("d",)))
    assert elsewhere is not None and elsewhere.first == 17.0


def test_no_first_turn_sample_is_the_uniform_reading() -> None:
    rows = [_srow(("x",), minutes=36.0, calls=3)]
    price = session_turn_price(rows, _session(("t",)))
    assert price == SessionTurnPrice(12.0, 12.0)
    assert price.note(3) == "floor 3x12.0m"


def test_the_row_round_trips_its_first_turn() -> None:
    row = _srow(("t",), minutes=24.0, calls=5, first=16.25)
    back = RouteLedgerRow.from_dict(json.loads(row.to_json()))
    assert back is not None and back.first_turn_minutes == 16.25
    legacy = {k: v for k, v in row.to_dict().items() if k != "first_turn_minutes"}
    old = RouteLedgerRow.from_dict(legacy)
    assert old is not None and old.first_turn_minutes is None


# --- the bound on the share-cut exemption -----------------------------------

def _rank(run: RouteRun, rows: list[RouteLedgerRow], **kw: Any) -> str:
    s = schedule([run], rows, oracle_fp="o", hollow={}, session_min_turns=3, **kw)
    return [*s.ordered, *s.deferred][0].reason


def test_one_cut_is_owed_a_look_and_a_second_in_a_row_is_graded() -> None:
    """`t` cut under one packing, then under another: the second cut is graded
    on its outcome, whichever set it rode in."""
    cut1 = _srow(("t", "u"), minutes=20.0, calls=1, share=12.0, started="1")
    cut2 = _srow(("t", "v"), minutes=25.0, calls=1, share=13.0, started="2")
    run = _session(("t",), targets=("a",))
    assert _rank(run, [cut1]) == "served"
    assert _rank(run, [cut1, cut2]) == "rebuy_flat"
    # The unit path calls an unopened unit never_served whatever rides
    # with it. After two cuts the test is the re-buy anyway. After one
    # cut the look is still owed, so the unopened unit stays never_served.
    fresh = _session(("t",), targets=("a", "fresh"))
    assert _rank(fresh, [cut1, cut2]) == "rebuy_flat"
    assert _rank(fresh, [cut1]) == "never_served"
    # Control: a graded try on `t` between the cuts (another packing, it paid
    # `w`, not `t`) makes the second cut a first one again.
    full = _srow(("t", "w"), minutes=30.0, calls=8, share=30.0, started="1b",
                 outcome=RunOutcome.CASHED, delta=1, green=("w",))
    assert _rank(run, [cut1, full, cut2]) == "served"


def test_the_identity_row_of_a_second_cut_is_its_outcome() -> None:
    """The same run (identity) re-offered after a second cut on its test is
    the re-buy of what the cut recorded; after one cut it is served."""
    run = RouteRun(kind=RouteKind.SESSION, label="session t", bodies_sha="b",
                   targets=frozenset({FnLocator("src/x.rs", "a")}),
                   target_tests=frozenset({"t"}))
    cut1 = _srow(("t",), minutes=20.0, calls=1, share=12.0, started="1",
                 outcome=RunOutcome.REVERTED)
    cut2 = _srow(("t",), minutes=21.0, calls=2, share=13.0, started="2",
                 outcome=RunOutcome.REVERTED)
    assert _rank(run, [cut1]) == "served"
    assert _rank(run, [cut1, cut2]) == "rebuy_reverted"


# --- the test-keyed re-buy --------------------------------------------------

def test_a_repacked_slice_on_a_graded_unpaid_test_is_the_rebuy() -> None:
    """The graded try aimed at `t` and `u`; the next fill packs `t` alone over
    other units. The work key is new, so only the test can say it was
    bought."""
    graded = _srow(("t", "u"), minutes=51.0, calls=7, share=48.0, started="1",
                   targets=("a", "b"), green=())
    run = _session(("t",), targets=("b",))
    assert _rank(run, [graded]) == "rebuy_flat"
    # A slice that adds a test no session has tried is a new question.
    assert _rank(_session(("t", "new"), targets=("b",)), [graded]) == "served"
    # A later row on another route that paid the test clears it.
    paid = _srow(("t",), minutes=5.0, calls=1, started="2",
                 kind=RouteKind.FILE, outcome=RunOutcome.CASHED, delta=1,
                 green=("t",), targets=("b",))
    assert _rank(run, [graded, paid]) == "served"
    # A graded try that ran out of its share on a first cut is owed its look.
    cut = _srow(("t", "u"), minutes=20.0, calls=1, share=12.0, started="1",
                targets=("a", "b"), green=())
    assert _rank(run, [cut]) == "served"


def test_the_test_keyed_rebuy_is_readmitted_after_its_deferrals() -> None:
    graded = _srow(("t", "u"), minutes=51.0, calls=7, share=48.0, started="1",
                   targets=("a", "c"), green=())
    deferred = [_srow(("t",), minutes=0.0, calls=0, started=f"2{i}",
                      outcome=RunOutcome.DEFERRED, targets=("q",))
                for i in range(2)]
    run = _session(("t",), targets=("c",))
    assert _rank(run, [graded, deferred[0]], readmit_after=2) == "rebuy_flat"
    assert _rank(run, [graded, *deferred], readmit_after=2) == "rebuy_readmitted"


# --- the session records its first turn -------------------------------------

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                        container="impl fmt::Display for Report",
                        sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                        body_text="write!(f, \"{}\", self.msg)")}


def test_the_session_records_its_first_turn_from_its_opening(tmp_path: Path) -> None:
    """Opening (the baseline suite) and the first call are the first turn;
    the second call is not. Control: a one-turn session's first turn is
    the session."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("SESSION_LIB")
    clock = [0.0]
    replies = ['<FN name="fmt" block="impl fmt::Display for Report">NEW</FN>', ""]
    red = TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=frozenset(), failed=frozenset({"t"}),
        pass_count=0, fail_count=1),))

    def measure(_raw: list[str], _role: object = None) -> TestStateReading:
        clock[0] += 60.0  # every suite reading costs a minute
        return red

    def ask(_m: object) -> str:
        clock[0] += 600.0 if len(replies) == 2 else 120.0
        return replies.pop(0)

    def splice(path: Path, container: str, name: str, body: str, where: Any,
               **_k: Any) -> SpliceOutcome:
        path.write_text(body)
        return SpliceOutcome(True, "ok", 0)

    def ports(asker: Any) -> SessionPorts:
        return SessionPorts(
            ask_turns=asker, splice=splice, check=lambda: (0, "", []),
            focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
            measure_suite=measure, read_source=lambda p: None,
            enumerate_bodies=lambda: {_FMT: (tmp_path / "src" / "lib.rs").read_text()},
            answer_pulls=lambda r, conversation="": None,
            log=lambda m: None, emit=lambda *a, **k: None,
            check_graceful_stop=lambda s: None,
            same_crash=SameCrashJudge(frozenset(), None),
            now=lambda: clock[0])

    out = run_slice_session(
        ports(ask), target_src_dir=tmp_path, cluster_name="t",
        slice_=SessionSlice(tests=("t",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=3)
    assert out.model_calls == 2
    # baseline 1 + first call 10 + its suite reading 1 = 12 minutes
    assert out.first_turn_minutes == 12.0

    clock[0] = 0.0
    (tmp_path / "src" / "lib.rs").write_text("SESSION_LIB")
    replies[:] = [""]
    one = run_slice_session(
        ports(lambda _m: (clock.__setitem__(0, clock[0] + 300.0), replies.pop(0))[1]),
        target_src_dir=tmp_path, cluster_name="t",
        slice_=SessionSlice(tests=("t",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=3)
    assert one.model_calls == 1 and one.first_turn_minutes == 6.0
