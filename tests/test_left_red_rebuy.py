"""A run whose aim the last try on its route ran and left red is that try
again, whatever the try's landing was graded.

Run `3366333-dfeac80c` (2026-10-29), fill 1: a pair was opened on two tests
its previous pair try (`1643490-850cf640`, iter 2) had aimed at, run and left
red. The pair's units were graded proven, so the rank took its served return
before any left-red question was asked (the question sat on the paying
returns only), and the pair ran 14.0 minutes flat. Replayed at plan time over
the route ledger since 2026-10-28 18:00 with the rule below: four opened
served/re-bought runs, 117.7 minutes, none paid.

The fact is the try's own: what it aimed at AND left red (`left_red_by`).
The row's `still_red` alone is the suite's still-failing set; on that ledger a
per-fn keep's set held 127–129 names, so reading it alone made almost any
later aim "left red" by a try that never aimed at it.

Claims (invented shape):
- the third of three tries ranks the flat re-buy when its aim sits inside the
  second's aim and left-red set, on the unit-evidence path (proven grades)
  and on the row path; the second, aiming at tests the first never aimed at,
  stays served;
- a try that opened the unit and landed nothing (an unchanged re-emit) is a
  try;
- a run's aim inside a previous try's `still_red` but outside that try's aim
  is a new question;
- the fill's premise gate refuses a claim whose remaining planned ground is
  all left red by the last try, and stands one whose remainder holds a test
  that try did not leave red.
Negative controls: the pre-fix rank (no question before the served return)
ranks the third try served; the pre-fix fact (aim ⊆ `still_red`) calls the
suite-wide row a re-buy; the gate without the run's reading stands the claim.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    _GroundGate,
)
from pipeline_core.domain import fill_schedule
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    UnitEvidence,
    UnitService,
    aim_left_red,
    left_red_by,
    schedule,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_outcome import PremiseEntry

_D = FnLocator("src/a.rs", "dispatch")
_I = FnLocator("src/b.rs", "implement")
_PAIR = (("src/a.rs", "dispatch"), ("src/b.rs", "implement"))
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  target_tests=frozenset({"t_q"}), bodies_sha="new")


def _pair(tests: set[str]) -> RouteRun:
    return RouteRun(kind=RouteKind.PAIR, label="pair dispatch + implement",
                    targets=frozenset({_D, _I}), target_tests=frozenset(tests),
                    files=("src/a.rs", "src/b.rs"), bodies_sha="now")


def _try(it: int, aim: tuple[str, ...], still: tuple[str, ...] | None, *,
         kept: bool = True) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=RouteKind.PAIR, label="pair dispatch + implement",
        targets=_PAIR, bodies_sha=f"b{it}", oracle_fp="o",
        outcome=RunOutcome.FLAT, kept=_PAIR if kept else (), opened=_PAIR,
        run_id="r", iteration=it, started=f"2026-10-29T0{it}:00",
        target_tests=aim, newly_green=(), still_red=still,
        model_calls=1, minutes=10.0)


#: Try 1 aims at four tests and leaves them red; try 2 aims at one of them
#: and two it never aimed at, and leaves all three red (with the suite's
#: other reds); try 3 aims at two of try 2's three.
_T1 = _try(1, ("t_a", "t_dbg", "t_bt", "t_chain"),
           ("t_a", "t_dbg", "t_bt", "t_chain", "t_x"))
_T2 = _try(2, ("t_a", "t_box", "t_down"),
           ("t_a", "t_box", "t_down", "t_low", "t_mid", "t_x"))


def _ev(*rows: RouteLedgerRow) -> UnitEvidence:
    keys = {(r.run_id, r.iteration, u): (r.bodies_sha, "proven")
            for r in rows for u in (_D, _I)}
    return UnitEvidence(grades=keys)


def _reason(run: RouteRun, rows: list[RouteLedgerRow],
            ev: UnitEvidence | None) -> str:
    sched = schedule([run, _OTHER], rows, oracle_fp="o", hollow={},
                     unit_evidence=ev)
    return next(s.reason for s in (*sched.ordered, *sched.deferred)
                if s.run.kind is RouteKind.PAIR)


def test_the_third_try_on_the_aim_the_second_left_red_is_a_re_buy() -> None:
    third = _pair({"t_a", "t_down"})
    assert _reason(third, [_T1, _T2], _ev(_T1, _T2)) == "rebuy_flat"
    assert _reason(third, [_T1, _T2], None) == "rebuy_flat"
    # The second try asked tests the first never aimed at: a new question.
    second = _pair({"t_a", "t_box", "t_down"})
    assert _reason(second, [_T1], _ev(_T1)) == "served"


def test_negative_control_the_pre_fix_rank_never_asked_a_served_unit(
        monkeypatch) -> None:
    monkeypatch.setattr(UnitService, "last_measured_try",
                        lambda self, t, run: None)
    third = _pair({"t_a", "t_down"})
    assert _reason(third, [_T1, _T2], _ev(_T1, _T2)) == "served"


def test_a_try_that_landed_nothing_is_still_a_try() -> None:
    reemit = _try(2, ("t_a", "t_box", "t_down"),
                  ("t_a", "t_box", "t_down"), kept=False)
    assert _reason(_pair({"t_a", "t_down"}), [_T1, reemit],
                   _ev(_T1)) == "rebuy_flat"


def test_a_suite_red_test_the_try_never_aimed_at_is_a_new_question() -> None:
    # t_low and t_mid are in try 2's still_red (the suite's reds) but not in
    # its aim.
    run = _pair({"t_low", "t_mid"})
    assert left_red_by(_T2) == frozenset({"t_a", "t_box", "t_down"})
    assert not aim_left_red(run, _T2)
    assert _reason(run, [_T1, _T2], _ev(_T1, _T2)) == "served"
    # Negative control: the pre-fix fact read the suite's set alone.
    assert {"t_low", "t_mid"} <= set(_T2.still_red or ())


def test_a_later_paying_row_on_the_unit_makes_the_next_try_new() -> None:
    paid = RouteLedgerRow(
        kind=RouteKind.FILE, label="file src/a.rs",
        targets=(("src/a.rs", "dispatch"),), bodies_sha="f", oracle_fp="o",
        outcome=RunOutcome.CASHED, net_pass_delta=2,
        kept=(("src/a.rs", "dispatch"),), opened=(("src/a.rs", "dispatch"),),
        run_id="r", iteration=3, started="2026-10-29T03:00",
        target_tests=("t_other",), newly_green=("t_other",), still_red=(),
        model_calls=1, minutes=5.0)
    units = UnitService([_T1, _T2, paid], "o")
    third = _pair({"t_a", "t_down"})
    assert units.last_measured_try(_D, third) is None
    assert units.last_measured_try(_I, third) is _T2


def test_the_schedule_hands_a_served_run_its_left_red_reading() -> None:
    second = _pair({"t_a", "t_box", "t_down"})
    sched = schedule([second, _OTHER], [_T1], oracle_fp="o", hollow={},
                     unit_evidence=_ev(_T1))
    run = next(s.run for s in (*sched.ordered, *sched.deferred)
               if s.run.kind is RouteKind.PAIR)
    assert dict(run.left_red) == {
        _D: frozenset({"t_a", "t_dbg", "t_bt", "t_chain"}),
        _I: frozenset({"t_a", "t_dbg", "t_bt", "t_chain"})}


def _gate(plan: dict[str, PremiseEntry],
          now: dict[str, PremiseEntry]) -> _GroundGate:
    gate = _GroundGate(FillExecutionDeps(
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        check_stop=lambda _c: None, ledger=None, project="p", cluster="c",
        run_id="r", iteration=1, oracle_fp="o",
        premise=plan, ground_now=lambda: now))
    fill_reading.note_kept(0)
    return gate


def test_a_claim_standing_only_on_what_the_last_try_left_red_is_refused(
        ) -> None:
    """The plan's run aimed at t_a, t_down and t_new; a keep of the fill
    moved t_new. What stands is what the last try on this route left red."""
    stub = ("fn", "src/z.rs", "s", "")
    plan: dict[str, PremiseEntry] = dict.fromkeys(("t_a", "t_down", "t_new"), (False, stub, False))
    now = {**plan, "t_new": (True, ("ok",), False)}
    scope = frozenset({"t_a", "t_down", "t_new"})
    units = frozenset({_D, _I})
    left = {_D: frozenset({"t_a", "t_box", "t_down"})}
    assert _gate(plan, now).for_run(left)(scope, units)
    # The remainder holds a test that try did not leave red: it stands.
    assert not _gate(plan, now).for_run({_D: frozenset({"t_a"})})(
        scope, units)
    # Negative control: the gate without the run's reading stands it.
    assert not _gate(plan, now)(scope, units)


def test_the_paying_returns_no_longer_carry_their_own_question() -> None:
    """One question, asked once (`UnitService._target_class`)."""
    import inspect
    src = inspect.getsource(fill_schedule.UnitService)
    assert src.count("aim_left_red(") == 1


def _proven(row: RouteLedgerRow, units: frozenset[FnLocator]) -> UnitEvidence:
    # No comparison: a proven grade is the tree, not a payment, so the
    # landing alone is served. The ask is what demotes.
    keys = {(row.run_id, row.iteration, u): (row.bodies_sha, "proven")
            for u in units}
    return UnitEvidence(grades=keys)


def _sched(run: RouteRun, rows: list[RouteLedgerRow], ev: UnitEvidence, *,
           budget: float = 0.0):
    return schedule([run, _OTHER], rows, oracle_fp="o", hollow={},
                    unit_evidence=ev, budget_minutes=budget)


def _of(sched, label: str):
    return next(s for s in (*sched.ordered, *sched.deferred)
                if s.run.label == label)


def test_a_narrow_aim_inside_another_files_leftover_stays_served() -> None:
    """A try that opened other files left a large leftover. A later run of
    one of those files, aiming at one name inside it, is not that try.
    Proven grades: without the question the landing would be served, and
    the wide leftover used to make it the flat re-buy."""
    one, two = FnLocator("src/a.rs", "one"), FnLocator("src/a.rs", "two")
    opened = (("src/a.rs", "one"), ("src/a.rs", "two"),
              *[(f"src/f{i}.rs", "u") for i in range(6)])
    names = tuple(f"t{i}" for i in range(120))
    row = RouteLedgerRow(
        kind=RouteKind.PER_FN, label="per-fn wide", targets=opened,
        opened=opened, bodies_sha="w", oracle_fp="o",
        outcome=RunOutcome.FLAT, kept=opened, run_id="r", iteration=1,
        started="2026-10-29T01:00", target_tests=names, newly_green=(),
        still_red=names, model_calls=1, minutes=40.0)
    run = RouteRun(kind=RouteKind.PER_FN, label="per-fn narrow",
                   targets=frozenset({one, two}),
                   target_tests=frozenset({"t0"}), files=("src/a.rs",),
                   bodies_sha="n")
    ev = _proven(row, frozenset({one, two}))
    assert not aim_left_red(run, row)
    sched = _sched(run, [row], ev, budget=200)
    got = _of(sched, "per-fn narrow")
    assert got.reason == "served" and got in sched.ordered
    assert got.run.left_red == ()


def test_dropping_a_unit_in_the_same_files_on_a_leftover_aim_is_a_re_buy(
        ) -> None:
    """The later run writes a subset of the units the try opened, in the
    same files, and its aim sits in that try's leftover. Proven grades, so
    the landing alone would serve it."""
    opened = (("src/a.rs", "one"), ("src/a.rs", "two"), ("src/a.rs", "three"))
    names = tuple(f"t{i}" for i in range(10))
    row = RouteLedgerRow(
        kind=RouteKind.FILE, label="file src/a.rs", targets=opened,
        opened=opened, bodies_sha="w", oracle_fp="o",
        outcome=RunOutcome.FLAT, kept=opened, run_id="r", iteration=1,
        started="2026-10-29T01:00", target_tests=names, newly_green=(),
        still_red=names, model_calls=1, minutes=20.0)
    keep = frozenset({FnLocator("src/a.rs", "one"), FnLocator("src/a.rs", "two")})
    run = RouteRun(kind=RouteKind.FILE, label="file src/a.rs", targets=keep,
                   target_tests=frozenset({"t0", "t1", "t2", "t3"}),
                   files=("src/a.rs",), bodies_sha="n")
    assert aim_left_red(run, row)
    sched = _sched(run, [row], _proven(row, keep), budget=200)
    got = _of(sched, "file src/a.rs")
    assert got.reason == "rebuy_flat" and got in sched.deferred
    # A unit the file try did not open, already served on another route,
    # makes the run a new question. The counts can be close and the aim
    # can sit inside the leftover; the unit sets are not nested.
    four = FnLocator("src/a.rs", "four")
    elsewhere = RouteLedgerRow(
        kind=RouteKind.PER_FN, label="per-fn four",
        targets=(("src/a.rs", "four"),), opened=(("src/a.rs", "four"),),
        bodies_sha="p", oracle_fp="o", outcome=RunOutcome.FLAT,
        kept=(("src/a.rs", "four"),), run_id="r", iteration=2,
        started="2026-10-29T02:00", target_tests=("t0",), newly_green=(),
        still_red=("t0",), model_calls=1, minutes=5.0)
    wider = RouteRun(kind=RouteKind.FILE, label="file src/a.rs wider",
                     targets=keep | {four}, target_tests=frozenset({"t0"}),
                     files=("src/a.rs",), bodies_sha="n")
    assert not aim_left_red(wider, row)
    again = _sched(wider, [row, elsewhere], _proven(row, keep), budget=200)
    fresh = _of(again, "file src/a.rs wider")
    assert fresh.reason == "served" and fresh.run.left_red == ()
