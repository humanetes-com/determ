"""A claim whose planned ground moved still stands when the ground now stops
one of its planned tests inside the unit it writes.

Recorded fixture: run `790176-f0df7402` (2026-09-23, cluster `stable`),
fills 1 and 2. `plan_holds` / `now_holds` are the sweeps' stub holds
(`phase_c.crash.stub_holds`, iter 1→2 for fill 1, iter 2→3 for fill 2: each
failing test under the still-stubbed fn it first fails in; the event caps a
unit's list at 50 names). `refused` is `phase_c.fn.unclaimed` `premise` for
the fill. `scope` is each refused unit's attributed tests as that iteration's
`[attribution/escalate]` lines name them — a lower bound of the dispatch map;
an empty scope is asked on the whole ground, as the per-fn worker does for a
unit with no dispatched test.

Live: fill 1's keep of `src/ptr.rs::new` moved all 141 failing tests and the
gate refused 10 units; the iter-2 sweep read `downcast_ref` holding 6 and
`is` holding 4 at their first failure; fill 2 refused them (and
`downcast_mut`, `from`) again, and the iter-3 sweep read the same holds.

The plan's sentence: a unit the moved tests now first-fail in is claimed;
one whose tests now stop in another unit is still refused. Negative control:
the gate asked with no unit — the pre-fix question — refuses every one.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    _GroundGate,
)
from pipeline_core.domain.fill_schedule import RunClock
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_outcome import PremiseEntry, standing_in

_FIXTURE = (Path(__file__).parent / "fixtures" / "premise"
            / "790176-f0df7402_fills12.json")


def _loc(key: str) -> FnLocator:
    file, _, fn = key.partition("::")
    return FnLocator(file, fn)


def _site(key: str) -> tuple[str, ...]:
    file, _, fn = key.partition("::")
    return ("fn", file, fn, "")


def _grounds(fx: dict) -> tuple[dict[str, PremiseEntry], dict[str, PremiseEntry]]:
    plan = {t: (False, _site(u), False)
            for u, ts in fx["plan_holds"].items() for t in ts}
    now = {t: (False, _site(u), False)
           for u, ts in fx["now_holds"].items() for t in ts}
    for t in plan:
        # A test the next sweep names under no stub stopped somewhere else.
        now.setdefault(t, (False, ("crate", "elsewhere"), False))
    return plan, now


def _gate(plan: dict[str, PremiseEntry],
          now: dict[str, PremiseEntry]) -> _GroundGate:
    gate = _GroundGate(FillExecutionDeps(
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        check_stop=lambda _c: None, ledger=None, project="p", cluster="c",
        run_id="r", iteration=1, oracle_fp="o",
        premise=plan, ground_now=lambda: now))
    fill_reading.note_kept(0)  # a keep since the gate's last look
    return gate


def _refused(fill: str, *, with_units: bool) -> set[str]:
    rec = json.loads(_FIXTURE.read_text())
    fx = rec["fills"][fill]
    plan, now = _grounds(fx)
    gate = _gate(plan, now)
    out: set[str] = set()
    for u in fx["refused"]:
        scope = frozenset(fx["scope"][u])
        units = frozenset({_loc(u)}) if with_units else frozenset()
        if gate(scope, units):
            out.add(u)
    return out


def test_fill1_units_the_moved_tests_now_first_fail_in_are_claimed() -> None:
    assert _refused("1", with_units=True) == {
        "src/context.rs::source", "src/ensure.rs::new",
        "src/wrapper.rs::source", "src/error.rs::backtrace"}


def test_fill2_holders_whose_own_holds_did_not_move_are_claimed() -> None:
    assert _refused("2", with_units=True) == set()


def test_negative_control_the_unitless_question_refuses_every_live_refusal(
        ) -> None:
    assert _refused("1", with_units=False) == set(
        json.loads(_FIXTURE.read_text())["fills"]["1"]["refused"])
    assert _refused("2", with_units=False) >= {
        "src/error.rs::downcast_mut", "src/error.rs::from",
        "src/error.rs::is"}


# ---- invented shape ----


def test_a_chain_link_moves_tests_onto_one_unit_and_off_another() -> None:
    """Three stubs a → b, c: a keep of `a` moves t1 onto b and t2 onto a
    unit outside the plan. b is claimed; c, planned on t2, is refused."""
    plan: dict[str, PremiseEntry] = {
        "t1": (False, ("fn", "src/x.rs", "a", ""), False),
        "t2": (False, ("fn", "src/x.rs", "a", ""), False),
        "t3": (False, ("at", "tests/t.rs", "4"), False),
    }
    now = {**plan,
           "t1": (False, ("fn", "src/x.rs", "b", "impl B"), False),
           "t2": (False, ("fn", "src/y.rs", "z", ""), False)}
    gate = _gate(plan, now)
    assert not gate(frozenset({"t1", "t2"}), frozenset({_loc("src/x.rs::b")}))
    assert gate(frozenset({"t2"}), frozenset({_loc("src/x.rs::c")}))
    # A run-scoped claim writing b stands on the same reading.
    assert not gate(None, frozenset({_loc("src/x.rs::b"),
                                     _loc("src/x.rs::c")}))
    # t3 did not move: the claim stands on the part of its ground the
    # plan's reading still describes (`test_outcome.ground_left`).
    assert not gate(frozenset({"t3", "t2"}), frozenset({_loc("src/x.rs::c")}))
    # Once t3 moves too, its new stop is in test source, which names no
    # unit, so nothing stands the claim.
    gate2 = _gate(plan, {**now, "t3": (False, ("at", "tests/t.rs", "9"), False)})
    assert gate2(frozenset({"t3", "t2"}), frozenset({_loc("src/x.rs::c")}))


def test_a_copy_in_another_binary_counts() -> None:
    now: dict[str, PremiseEntry] = {
        "t": (False, ("at", "tests/a.rs", "3", "+", "fn|src/x.rs|b|"),
              False)}
    assert standing_in(now, {("src/x.rs", "b")}) == ("t",)
    assert standing_in(now, {("src/x.rs", "c")}) == ()


def test_a_passing_test_supports_nothing() -> None:
    now: dict[str, PremiseEntry] = {
        "t": (True, ("fn", "src/x.rs", "b", ""), False)}
    assert standing_in(now, {("src/x.rs", "b")}) == ()


def test_a_test_the_claim_was_not_planned_on_stands_it() -> None:
    """Run `14235-bf24f931` fill 1's shape: the claim on `b` was planned on
    t1, a keep moved t1 to another unit and moved t2 (planned on nothing
    here) to its first failure inside `b`. The claim stands. Control: the
    same keep with t2 moved anywhere else refuses it, as before."""
    plan: dict[str, PremiseEntry] = {
        "t1": (False, ("fn", "src/x.rs", "a", ""), False),
        "t2": (False, ("fn", "src/y.rs", "f", ""), False),
    }
    now = {"t1": (False, ("fn", "src/x.rs", "d", ""), False),
           "t2": (False, ("fn", "src/x.rs", "b", "mod m"), False)}
    claim = (frozenset({"t1"}), frozenset({_loc("src/x.rs::b")}))
    assert not _gate(plan, now)(*claim)
    elsewhere = {**now, "t2": (False, ("fn", "src/z.rs", "g", ""), False)}
    assert _gate(plan, elsewhere)(*claim)


def test_the_clock_asks_with_the_runs_units_unless_a_claim_names_its_own(
        ) -> None:
    asked: list[frozenset[FnLocator]] = []

    def _gate_fn(_scope: frozenset[str] | None,
                 units: frozenset[FnLocator]) -> bool:
        asked.append(units)
        return False

    run_units = frozenset({_loc("src/x.rs::a"), _loc("src/x.rs::b")})
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None,
                     ground_moved=_gate_fn, units=run_units)
    clock.may_claim()
    clock.may_claim({"t"}, {_loc("src/x.rs::b")})
    assert asked == [run_units, frozenset({_loc("src/x.rs::b")})]


def test_a_unit_refused_on_premise_last_fill_is_not_refused_again() -> None:
    """Since 2026-10-01 eight units were refused on premise in consecutive
    fills (`src/ensure.rs::render` and `as_str` on `14235-bf24f931` fills 3
    and 4): the re-plan chose them again and another run's keep moved their
    planned tests first again. A unit the previous fill refused on premise
    stands this time. Control: without the record it is refused."""
    plan: dict[str, PremiseEntry] = {
        "t1": (False, ("fn", "src/x.rs", "a", ""), False)}
    now = {"t1": (False, ("fn", "src/x.rs", "d", ""), False)}
    claim = (frozenset({"t1"}), frozenset({_loc("src/x.rs::b")}))
    gate = _GroundGate(FillExecutionDeps(
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        check_stop=lambda _c: None, ledger=None, project="p", cluster="c",
        run_id="r", iteration=2, oracle_fp="o",
        premise=plan, ground_now=lambda: now),
        frozenset({_loc("src/x.rs::b")}))
    fill_reading.note_kept(0)
    assert not gate(*claim)
    assert _gate(plan, now)(*claim)


def test_the_previous_fill_is_the_newest_earlier_one_and_only_its_premise_drops(
        ) -> None:
    from pipeline_core.domain.fill_schedule import (
        RouteKind,
        RouteLedgerRow,
        RunOutcome,
        premise_refused_last_fill,
    )

    def row(run: str, it: int, started: str,
            filtered: tuple[tuple[str, str, str], ...]) -> RouteLedgerRow:
        return RouteLedgerRow(
            kind=RouteKind.PER_FN, label="per-fn", targets=(), bodies_sha="",
            oracle_fp="o", outcome=RunOutcome.FLAT, run_id=run, iteration=it,
            cluster="c", started=started, filtered=filtered)

    rows = [
        row("r1", 3, "2026-10-16T05:00:00", (("src/x.rs", "old", "premise"),)),
        row("r2", 1, "2026-10-16T06:00:00", (("src/x.rs", "b", "premise"),
                                              ("src/x.rs", "s", "share"))),
        row("r2", 2, "2026-10-16T07:00:00", (("src/x.rs", "now", "premise"),)),
    ]
    got = premise_refused_last_fill(rows, cluster="c", run_id="r2", iteration=2)
    assert got == {_loc("src/x.rs::b")}
    assert premise_refused_last_fill(rows, cluster="other", run_id="r2",
                                     iteration=2) == frozenset()
