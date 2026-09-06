"""The fill schedule's three rules, each with its negative control.

The sentences under test are the plan's, not the implementation's:
"a run that fills a stub which makes a leftover test hollow goes ahead of
runs that fill no such stub"; "a run with no ledger row on its identity
ranks ahead of a re-buy"; "a re-buy is deferred unless every candidate is
one or it was deferred `readmit_after` times"; "a run that cashed on its
last claim may extend past its share; one that produced nothing stops".
The recorded-fixture tests (`tests/test_fill_schedule_recorded.py`) pin
the same sentences on the live 2026-09-06 fills.
"""
from __future__ import annotations

from pipeline_core.domain.fill_schedule import (
    TODAY_ORDER,
    FillBudget,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
    bodies_sha_of,
    hollow_by_test,
    rebuy_identity,
    run_blocks,
    schedule,
)
from pipeline_core.domain.models import FnLocator, FnSite

_STUB = FnLocator("src/x.rs", "drop_rest")
_FMT = FnLocator("src/x.rs", "fmt")
_NEW = FnLocator("src/y.rs", "new")


def _session(label: str, tests: set[str], units: set[FnLocator], seq: int = 0) -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label=label, targets=frozenset(units),
                    target_tests=frozenset(tests), files=("src/x.rs",), seq=seq,
                    bodies_sha="s")


def _file(label: str, units: set[FnLocator], stubs: set[FnLocator],
          tests: set[str] = frozenset(), sha: str = "f") -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label=label, targets=frozenset(units),
                    target_tests=frozenset(tests), stubs_in_targets=frozenset(stubs),
                    files=("src/x.rs",), bodies_sha=sha)


def _per_fn(stubs: set[FnLocator]) -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN, label="per-fn", targets=frozenset(stubs),
                    stubs_in_targets=frozenset(stubs), bodies_sha="p")


def _hollow() -> dict[str, frozenset[FnLocator]]:
    slices = {
        "t_hollow": {FnSite("src/x.rs", "drop_rest", ""), FnSite("src/x.rs", "fmt", "impl Debug for X")},
        "t_fmt": {FnSite("src/x.rs", "fmt", "impl Debug for X")},
    }
    return hollow_by_test(slices, {_STUB})


def test_hollow_by_test_names_the_blocking_stubs() -> None:
    h = _hollow()
    assert h == {"t_hollow": frozenset({_STUB})}


def test_a_stub_run_that_hollows_a_leftover_test_goes_before_a_stub_free_session() -> None:
    """Plan sentence: blockers first. Control: today's fixed order puts the
    session first."""
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT})
    file_run = _file("file src/x.rs", {_STUB, _FMT}, {_STUB}, {"t_hollow", "t_fmt"})
    per_fn = _per_fn({_STUB})
    runs = [session, file_run, per_fn]
    sched = schedule(runs, [], oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == [
        "file src/x.rs", "per-fn", "session[1 test(s)]"]
    assert sched.ordered[0].level == 0 and sched.ordered[2].level == 1
    # Negative control: the pre-schedule walk.
    fixed = sorted(runs, key=lambda r: TODAY_ORDER.index(r.kind))
    assert fixed[0].kind is RouteKind.SESSION


def test_with_no_hollow_test_the_session_keeps_todays_precedence() -> None:
    """The 91-plateau shape: no stub blocks any leftover test, so nothing
    is demoted and the session opens first."""
    session = _session("session[1 test(s)]", {"t_fmt"}, {_FMT})
    file_run = _file("file src/x.rs", {_FMT}, set(), {"t_fmt"})
    sched = schedule([file_run, session], [], oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [
        "session[1 test(s)]", "file src/x.rs"]


def test_a_run_whose_tests_are_hollow_goes_after_the_run_holding_its_stub() -> None:
    holder = _file("file src/x.rs", {_STUB}, {_STUB}, {"t_hollow"})
    consumer = RouteRun(kind=RouteKind.IMPL, label="impl src/y.rs",
                        targets=frozenset({_NEW}), target_tests=frozenset({"t_hollow"}),
                        stubs_in_targets=frozenset({_NEW}), bodies_sha="i")
    assert run_blocks(holder, consumer, _hollow())
    assert not run_blocks(consumer, holder, _hollow())
    sched = schedule([consumer, holder], [], oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == ["file src/x.rs", "impl src/y.rs"]
    assert sched.ordered[1].blocked_by == ("file src/x.rs",)


def _row(run: RouteRun, outcome: RunOutcome, delta: int = 0,
         oracle: str = "o") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=run.kind, label=run.label,
        targets=tuple(sorted((f, fn) for f, fn in run.targets)),
        bodies_sha=run.bodies_sha, oracle_fp=oracle, outcome=outcome,
        net_pass_delta=delta)


def test_a_flat_rebuy_sinks_below_a_never_served_run_and_is_deferred_under_a_budget() -> None:
    """Plan sentence: never-served before re-buys; a zero-weight re-buy is
    deferred. Control: with no ledger the same run keeps its rank."""
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow())
    assert [s.run.label for s in sched.ordered] == ["per-fn", "file src/x.rs"]
    assert sched.ordered[1].reason == "rebuy_flat"
    budgeted = schedule([file_run, per_fn], rows, oracle_fp="o",
                        hollow=_hollow(), budget_minutes=60)
    assert [s.run.label for s in budgeted.ordered] == ["per-fn"]
    assert [s.run.label for s in budgeted.deferred] == ["file src/x.rs"]
    assert budgeted.ordered[0].share_minutes == 60.0
    control = schedule([file_run, per_fn], [], oracle_fp="o", hollow=_hollow(),
                       budget_minutes=60)
    assert [s.run.label for s in control.ordered] == ["file src/x.rs", "per-fn"]
    assert control.ordered[0].reason == "never_served"
    assert control.ordered[0].share_minutes == 30.0


def test_a_paying_run_keeps_the_never_served_rank() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.CASHED, 3)]
    sched = schedule([per_fn, file_run], rows, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].run.label == "per-fn"  # never-served first
    assert sched.ordered[1].reason == "paying" and sched.ordered[1].weight == 2


def test_a_moved_body_is_a_new_try_not_a_rebuy() -> None:
    """The identity carries the bodies: the same targets on different bodies
    is `served`, ranked between never-served and a re-buy."""
    before = _file("file src/x.rs", {_STUB}, {_STUB}, sha="old")
    after = _file("file src/x.rs", {_STUB}, {_STUB}, sha="new")
    rows = [_row(before, RunOutcome.FLAT, 0)]
    assert rebuy_identity(after.kind, after.targets, after.bodies_sha, "o") != rows[0].identity
    sched = schedule([after], rows, oracle_fp="o", hollow=_hollow())
    assert sched.ordered[0].reason == "served" and sched.ordered[0].weight == 1


def test_a_rebuy_deferred_readmit_after_times_is_readmitted_with_weight_one() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0),
            _row(file_run, RunOutcome.DEFERRED), _row(file_run, RunOutcome.DEFERRED)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=60, readmit_after=2)
    labels = [s.run.label for s in sched.ordered]
    assert "file src/x.rs" in labels and not sched.deferred
    readmitted = next(s for s in sched.ordered if s.run.label == "file src/x.rs")
    assert readmitted.reason == "rebuy_readmitted" and readmitted.weight == 1
    once = rows[:2]
    sched_once = schedule([file_run, per_fn], once, oracle_fp="o", hollow=_hollow(),
                          budget_minutes=60, readmit_after=2)
    assert [s.run.label for s in sched_once.deferred] == ["file src/x.rs"]


def test_when_every_candidate_is_a_rebuy_the_budget_is_shared_not_spun() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0), _row(per_fn, RunOutcome.REVERTED, -2)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=60)
    assert not sched.deferred
    assert {s.reason for s in sched.ordered} == {"rebuy_readmitted"}
    assert sum(s.share_minutes or 0 for s in sched.ordered) == 60.0


def test_an_oracle_move_makes_every_row_a_different_identity() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0, oracle="old")]
    sched = schedule([file_run], rows, oracle_fp="new", hollow=_hollow())
    assert sched.ordered[0].reason == "served"  # same targets, different try


def test_order_only_under_a_zero_budget_defers_nothing() -> None:
    file_run = _file("file src/x.rs", {_STUB}, {_STUB})
    per_fn = _per_fn({_STUB})
    rows = [_row(file_run, RunOutcome.FLAT, 0)]
    sched = schedule([file_run, per_fn], rows, oracle_fp="o", hollow=_hollow(),
                     budget_minutes=0)
    assert len(sched.ordered) == 2 and not sched.deferred
    assert all(s.share_minutes is None for s in sched.ordered)


def test_bodies_sha_folds_whitespace_and_ignores_order() -> None:
    a = bodies_sha_of([(_STUB, "let x = 1;\n  y"), (_FMT, "z")])
    b = bodies_sha_of([(_FMT, "z"), (_STUB, "let x = 1; y")])
    assert a == b
    assert a != bodies_sha_of([(_STUB, "let x = 2; y"), (_FMT, "z")])


def test_row_round_trips_through_json() -> None:
    run = _file("file src/x.rs", {_STUB}, {_STUB})
    row = _row(run, RunOutcome.FLAT, 0)
    back = RouteLedgerRow.from_dict(__import__("json").loads(row.to_json()))
    assert back == row


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_run_clock_claims_within_share_extends_only_while_paying() -> None:
    """Plan sentence: budget follows payoff. An unpaid run past its share
    stops claiming; a paid one extends into the fill's unspent budget."""
    now = _Clock()
    budget = FillBudget(100, now=now)
    clock = RunClock(share_minutes=10, fill_remaining=budget.remaining, now=now)
    assert clock.may_claim()
    now.t = 11 * 60
    assert not clock.may_claim()  # share spent, nothing paid
    clock.note_delta(3)
    assert clock.may_claim()      # paying: extends into the fill budget
    now.t = 101 * 60
    assert not clock.may_claim()  # the fill budget is gone too
    assert clock.refused == 2


def test_an_unbounded_clock_always_claims_and_shares_flow_forward() -> None:
    now = _Clock()
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None, now=now)
    now.t = 10_000
    assert clock.may_claim()
    now.t = 0
    budget = FillBudget(60, now=now)
    assert budget.share_for(2, 4) == 30.0
    now.t = 30 * 60  # the first run finished early: 30 min unspent of 60
    assert budget.share_for(2, 2) == 30.0  # flows to the last run
    assert budget.share_for(0, 2) == 0.0
    unbounded = FillBudget(0, now=now)
    assert unbounded.share_for(2, 2) is None
