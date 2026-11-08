"""A red test that the policy's level cannot turn green owes its functions at
the maximum level, once per function per red episode; the schedule sends the
runs holding them at the maximum; the record says which level an ask was
sent at.

The trigger is the TEST's count of owed asks below the maximum (landings and
planned misses, any function, any fingerprint), not a pair's. On the anyhow
record of 2026-10-09 → 11-07 (51 runs, 10,263 owed landings) a landing moved
the test's failure text 7,942 times and the walk then rarely owed the same
function again: 201 of 3,666 red-at-end tests ever had a function with two
owed landings (`scripts/owed_landing_text_moves.py`), so a per-pair count of
two would have escalated about one stuck test in eighteen.

Shapes are invented: no crate, file, test or symbol of a live run.
"""
from __future__ import annotations

from pipeline_core.domain.candidate_tiers import (
    MAX_FOLD,
    AskLedger,
    _fold,
    due_reading,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    carve_levels,
    rebuy_identity,
    schedule,
)
from pipeline_core.domain.model_profile import EffortLevel
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.red_ledger import RedClass, red_ledger
from pipeline_core.domain.test_sight import SightReading

MAX = EffortLevel.MAX
MED = EffortLevel.MEDIUM

A, B, C = "src/a.rs::alpha", "src/a.rs::beta", "src/b.rs::gamma"
T = "test_widget_reports_shape"


def _reading(ledger: AskLedger, *, evidence: str, after: int, tree: str = "t1",
             slices: dict[str, tuple[str, ...]] | None = None):
    return due_reading(
        [T], slices=slices or {T: (A, B, C)}, reach={}, evidence={T: evidence},
        held={}, ledger=ledger, tree=tree, escalate_after=after)


def _ask_round(ledger: AskLedger, reading, *, level: EffortLevel,
               landed: bool) -> None:
    """One fill that offered every owed function of `T` at `level`."""
    r = reading.by_test[T]
    for u, fp in r.owed.items():
        ledger.count_ask(u, T, level, landed=landed,
                         owed_max=reading.owes_max(u, T))
        if landed:
            ledger.record(u, T, fp)
        else:
            ledger.miss(u, T, fp, "no_landing")


def test_asks_below_the_maximum_escalate_the_test_across_text_changes():
    led = AskLedger()
    r1 = _reading(led, evidence="left: 1 right: 2", after=3)
    assert not r1.max_units
    _ask_round(led, r1, level=MED, landed=True)          # 3 owed asks
    # The text moved on a new tree: a new fingerprint, the walk back at tier
    # 0; the count is the test's and carries over.
    r2 = _reading(led, evidence="left: 7 right: 2", after=3, tree="t2")
    assert set(r2.by_test[T].levels.values()) == {MAX}
    assert r2.max_units == {FnLocator(*u.rsplit("::", 1)) for u in r2.by_test[T].owed}


def test_planned_misses_count_and_unplanned_offers_do_not():
    led = AskLedger()
    led.count_ask(A, T, MED, landed=False, planned=False)
    assert led.low_asks.get(T, 0) == 0
    for _ in range(2):
        led.count_ask(A, T, MED, landed=False, planned=True)
    assert led.escalated(T, 2)


def test_one_maximum_try_per_function_per_red_episode():
    led = AskLedger()
    led.low_asks[T] = 5
    assert led.level_for(A, T, after=5) is MAX
    led.count_ask(A, T, MAX, landed=True, owed_max=True)
    assert led.max_spent(A, T) and led.level_for(A, T, after=5) is None
    # More asks below the maximum do not re-escalate the spent pair.
    led.count_ask(A, T, MED, landed=True)
    assert led.level_for(A, T, after=5) is None
    assert led.level_for(B, T, after=5) is MAX
    # Green closes the episode: the count and the pair's try start again.
    led.green([T])
    assert led.low_asks.get(T) is None and not led.max_spent(A, T)
    assert led.level_for(A, T, after=5) is None


def test_a_maximum_run_spends_no_try_of_a_pair_nothing_owed_there():
    """A holder a maximum-level run claimed mid-fill, or a co-target of a
    unit route, was asked at the maximum without being owed there: counting
    it would suppress that pair's own escalation later."""
    led = AskLedger()
    led.count_ask(A, T, MAX, landed=True, owed_max=False)
    assert not led.max_spent(A, T) and led.low_asks.get(T, 0) == 0


def test_a_walk_spent_below_the_maximum_is_walked_again_at_it():
    led = AskLedger()
    r = _reading(led, evidence="boom", after=50, slices={T: (A,)})
    _ask_round(led, r, level=MED, landed=True)
    spent = _reading(led, evidence="boom", after=50, slices={T: (A,)})
    rt = spent.by_test[T]
    assert rt.levels == {A: MAX}
    assert rt.owed[A] == _fold(r.fingerprints[T], MAX_FOLD)
    assert not spent.exhausted
    _ask_round(led, spent, level=MAX, landed=True)
    assert _reading(led, evidence="boom", after=50, slices={T: (A,)}).exhausted


def test_negative_control_without_escalation_the_walk_ends_below_the_maximum():
    """`TDD_ESCALATE_AFTER=0`: the same walk is spent after its medium ask
    and the red ledger calls it the writer's limit at a level it was never
    asked at; with escalation that claim waits for a maximum try."""
    led = AskLedger()
    r = _reading(led, evidence="boom", after=0, slices={T: (A,)})
    _ask_round(led, r, level=MED, landed=True)
    done = _reading(led, evidence="boom", after=0, slices={T: (A,)})
    assert done.exhausted and not done.max_units
    sight = SightReading(taken=False)
    off = red_ledger([T], reading=done, ledger=led, sight=sight)
    assert off[0].klass is RedClass.MODEL_LIMIT
    on = red_ledger([T], reading=done, ledger=led, sight=sight, escalate_after=5)
    assert on[0].klass is RedClass.OPEN


def test_the_ledger_round_trips_the_episode():
    led = AskLedger()
    led.low_asks[T] = 4
    led.count_ask(A, T, MAX, landed=False, owed_max=True)
    back = AskLedger.from_dict(led.to_dict())
    assert (back.low_asks, back.max_tries) == (led.low_asks, led.max_tries)


# ── the schedule: which runs are sent at the maximum ─────────────────────────

_FA, _FB, _FC = (FnLocator(*u.rsplit("::", 1)) for u in (A, B, C))


def _per_fn() -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN, label="per-fn 3",
                    targets=frozenset({_FA, _FB, _FC}),
                    target_tests=frozenset({T}), files=("src/a.rs", "src/b.rs"),
                    unit_tests=((_FA, frozenset({T})), (_FB, frozenset({T})),
                                (_FC, frozenset({T}))), bodies_sha="s")


def test_a_per_unit_route_sends_only_its_owed_at_max_units_at_the_maximum():
    pieces = carve_levels(_per_fn(), {_FB})
    assert [(p.targets, p.escalated) for p in pieces] == [
        (frozenset({_FB}), True), (frozenset({_FA, _FC}), False)]


def test_a_transaction_goes_whole_and_nothing_owed_changes_nothing():
    file_run = RouteRun(kind=RouteKind.FILE, label="file src/a.rs",
                        targets=frozenset({_FA, _FB}), files=("src/a.rs",))
    assert [p.escalated for p in carve_levels(file_run, {_FB})] == [True]
    assert carve_levels(file_run, set()) == (file_run,)


def test_a_maximum_try_is_not_the_rebuy_of_the_same_bodies_at_medium():
    ident = rebuy_identity(RouteKind.FILE, {_FA}, "s", "o")
    assert rebuy_identity(RouteKind.FILE, {_FA}, "s", "o", escalated=True) != ident


def test_a_test_escalated_by_its_asks_puts_its_owed_unit_in_an_escalated_run():
    """The schedule reads `max_units` (`DueReading.max_units`): the run
    covering an owed-at-max unit opens escalated. Control: the same inputs
    with nothing owed at the maximum open no escalated run."""
    sched = schedule([_per_fn()], [], oracle_fp="o", hollow={},
                     due={_FB}, max_units={_FB})
    escalated = [s.run for s in sched.ordered if s.run.escalated]
    assert [r.targets for r in escalated] == [frozenset({_FB})]
    control = schedule([_per_fn()], [], oracle_fp="o", hollow={}, due={_FB})
    assert not any(s.run.escalated for s in control.ordered)
