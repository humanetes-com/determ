"""Service is a property of the UNIT, and a session's share is priced in turns.

The defect, run `450977-5f48b68a` (2026-09-21, cluster `stable`, oracle
`1c3b06644b2e`): the schedule called a run `never_served` when no ledger row
carried its WHOLE target list. The dispatch batch is re-drawn every
iteration, so file/impl/per-fn runs presented a new list almost every fill
and ranked first at double weight — fill 1's `file src/error.rs` over 5
targets bought 27/34 times (min/median), its per-fn re-fill over 12 targets
median 27 — while the slice session, whose aim stays put, ranked `served`,
got a 9–11 minute share against 11–14 minute turns, and ended after 2 of 8.

The recorded fixtures are fills 1 and 2 of that run with the ledger the live
schedule read (the route ledger index, cut at the fill's first own row).
The negative control of the rank half is the pre-change rule (a run is
served only if its whole (kind, targets) key has an opened row), which
reproduces the live `never_served` lines; of the floor half, the same fill
with `session_min_turns=0`.
"""
from __future__ import annotations

from collections.abc import Iterable

from fills.harness import candidate_runs, ledger_of, load_fill

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    schedule,
    session_turn_minutes,
)
from pipeline_core.domain.models import FnLocator

ORACLE = "1c3b06644b2e"
UNITS = [FnLocator("src/x.rs", n) for n in "abcdefghijkl"]


def _row(kind: RouteKind, targets: Iterable[FnLocator], outcome: RunOutcome, *,
         tests: tuple[str, ...] = (), minutes: float = 10.0, calls: int = 1,
         share: float | None = None, started: str = "") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label=f"{kind.value} run", bodies_sha="b",
        targets=tuple(sorted((t.file, t.fn_name) for t in targets)),
        oracle_fp="o", outcome=outcome, minutes=minutes, model_calls=calls,
        target_tests=tests, share_minutes=share, started=started)


def _per_fn(targets: Iterable[FnLocator], label: str = "per-fn") -> RouteRun:
    ts = frozenset(targets)
    return RouteRun(kind=RouteKind.PER_FN, label=label, targets=ts,
                    target_tests=frozenset(f"t_{u.fn_name}" for u in ts),
                    bodies_sha="new")


def _pre_change_rank_is_never_served(run: RouteRun,
                                     rows: list[RouteLedgerRow]) -> bool:
    """The rule this module replaces, for the negative control: with no row
    on the run's identity, `served` only if the whole (kind, targets) key
    has an opened row, else `never_served`."""
    key = (run.kind, frozenset(run.targets))
    return not any(
        (r.kind, frozenset(FnLocator(f, fn) for f, fn in r.targets)) == key
        for r in rows if r.outcome not in (
            RunOutcome.DEFERRED, RunOutcome.EMPTY, RunOutcome.UNMEASURED))


def _thirty_flat_rows() -> list[RouteLedgerRow]:
    """30 opened FLAT per-fn rows over units a..l, each a different batch."""
    rows = []
    for i in range(30):
        batch = [UNITS[(i + k) % 12] for k in range(4)]
        rows.append(_row(RouteKind.PER_FN, batch, RunOutcome.FLAT))
    return rows


def test_a_reshuffled_batch_of_bought_units_is_a_rebuy_not_never_served() -> None:
    rows = _thirty_flat_rows()
    batch = _per_fn([UNITS[1], UNITS[3], UNITS[5], UNITS[7], UNITS[9]])
    sched = schedule([batch], rows, oracle_fp="o", hollow={})
    assert sched.ordered[0].reason == "rebuy_flat"
    # Negative control: the whole-batch key has no row, so the old rule
    # printed never_served — the live line.
    assert _pre_change_rank_is_never_served(batch, rows)


def test_one_never_opened_unit_makes_the_batch_a_new_try() -> None:
    rows = _thirty_flat_rows()
    z = FnLocator("src/x.rs", "z")
    batch = _per_fn([UNITS[1], UNITS[3], z])
    assert schedule([batch], rows, oracle_fp="o", hollow={}).ordered[0].reason \
        == "never_served"


def test_a_starved_session_row_is_served_not_a_rebuy() -> None:
    """A session that ran out of its share before its minimum turns was
    ended by the share, not graded; with the floor off it is the re-buy."""
    tests = ("t_a",)
    units = UNITS[:3]
    starved = _row(RouteKind.SESSION, units, RunOutcome.REVERTED, tests=tests,
                   minutes=25.8, calls=2, share=20.0)
    run = RouteRun(kind=RouteKind.SESSION, label="session", bodies_sha="new",
                   targets=frozenset(units), target_tests=frozenset(tests))
    on = schedule([run], [starved], oracle_fp="o", hollow={}, session_min_turns=3)
    off = schedule([run], [starved], oracle_fp="o", hollow={}, session_min_turns=0)
    assert on.ordered[0].reason == "served"
    assert off.ordered[0].reason == "rebuy_reverted"


def test_the_session_floor_is_min_turns_at_its_turn_price() -> None:
    tests = ("t_a",)
    units = UNITS[:3]
    history = [_row(RouteKind.SESSION, units, RunOutcome.FLAT, tests=("t_x",),
                    minutes=36.0, calls=3)]
    run = RouteRun(kind=RouteKind.SESSION, label="session", bodies_sha="new",
                   targets=frozenset(units), target_tests=frozenset(tests))
    others = [_per_fn([FnLocator("src/y.rs", f"n{i}")], label=f"per-fn {i}")
              for i in range(10)]
    assert session_turn_minutes(history, run) == 12.0
    sched = schedule([run, *others], history, oracle_fp="o", hollow={},
                     budget_minutes=200, session_min_turns=3)
    sess = next(s for s in sched.ordered if s.run.kind is RouteKind.SESSION)
    assert sess.share_minutes is not None and sess.share_minutes >= 36.0
    assert "floor 3x12.0m" in sched.render()
    assert abs(sum(s.share_minutes or 0 for s in sched.ordered) - 200) < 1e-6
    # Control: the knob at 0 is the plain weighted split — the session is
    # "served" (weight 1) against ten never-served runs (weight 2): 200/21.
    plain = schedule([run, *others], history, oracle_fp="o", hollow={},
                     budget_minutes=200, session_min_turns=0)
    sess0 = next(s for s in plain.ordered if s.run.kind is RouteKind.SESSION)
    assert sess0.share_minutes is not None and sess0.share_minutes < 10.0


def test_floors_over_the_budget_are_clipped_and_said() -> None:
    tests = ("t_a",)
    runs, history = [], []
    for i in range(4):
        units = [FnLocator("src/s.rs", f"u{i}")]
        history.append(_row(RouteKind.SESSION, units, RunOutcome.FLAT,
                            tests=(f"t{i}x",), minutes=60.0, calls=3))
        runs.append(RouteRun(kind=RouteKind.SESSION, label=f"session {i}",
                             bodies_sha="new", targets=frozenset(units),
                             target_tests=frozenset((f"t{i}", *tests)), seq=i))
    sched = schedule(runs, history, oracle_fp="o", hollow={},
                     budget_minutes=200, session_min_turns=3)
    assert all(abs((s.share_minutes or 0) - 50.0) < 1e-6 for s in sched.ordered)
    assert "clipped to 50m" in sched.render()


def test_recorded_450977_fill_1_ranks_the_reshuffled_runs_as_rebuys() -> None:
    """Plan sentence: `file src/error.rs` and the 12-unit per-fn re-fill,
    printed never_served on the live line, rank `rebuy_flat` and are
    deferred under the 200-minute budget."""
    fix = load_fill("450977-5f48b68a_iter1")
    runs = [r for rs in candidate_runs(fix).values() for r in rs]
    rows = ledger_of(fix)
    sched = schedule(runs, rows, oracle_fp=ORACLE, hollow={}, budget_minutes=200,
                     max_per_kind={RouteKind.SESSION: 4}, session_min_turns=3)
    deferred = {s.run.label: s.reason for s in sched.deferred}
    assert deferred["file src/error.rs"] == "rebuy_flat"
    per_fn = next(lab for lab in deferred if lab.startswith("per-fn"))
    assert deferred[per_fn] == "rebuy_flat"
    assert deferred["session[5 test(s), 31 unit(s)] #0"] == "rebuy_flat"
    assert not [s for s in sched.ordered if s.reason == "never_served"]
    # Negative control: under the whole-batch key both were never served,
    # which is what the live schedule line printed.
    recorded = {str(r["label"]): str(r["reason"]) for r in fix["schedule_runs"]}
    assert recorded["file src/error.rs"] == "never_served"
    assert recorded[per_fn] == "never_served"


def test_recorded_450977_fill_2_opens_the_session_with_three_turns_of_share() -> None:
    """Reachability, not score: fill 1's session row (2 turns, 25.8 min on a
    20-min share) is starved, so fill 2's session opens with a share of at
    least 3 turns at its measured price; with the floor off it is the
    reverted re-buy and is deferred."""
    fix = load_fill("450977-5f48b68a_iter2")
    runs = [r for rs in candidate_runs(fix).values() for r in rs]
    rows = ledger_of(fix)
    kw = {"oracle_fp": ORACLE, "hollow": {}, "budget_minutes": 200,
          "max_per_kind": {RouteKind.SESSION: 4}}
    sched = schedule(runs, rows, session_min_turns=3, **kw)
    sess = next(s for s in sched.ordered if s.run.kind is RouteKind.SESSION)
    turn = session_turn_minutes(rows, sess.run)
    assert turn is not None and sess.share_minutes is not None
    assert sess.share_minutes >= 3 * turn - 1e-6
    off = schedule(runs, rows, session_min_turns=0, **kw)
    assert [s.reason for s in off.deferred if s.run.kind is RouteKind.SESSION] \
        == ["rebuy_reverted"]
    # The live schedule gave it 11 minutes.
    live = next(r for r in fix["schedule_runs"] if r["kind"] == "session")
    assert live["share_minutes"] is None or live["share_minutes"] < 3 * turn
