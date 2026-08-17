"""The GPU protector's window arithmetic (`domain/flat_score.py`).

Every case here is a reading sequence taken from a real run: the fixed point the
guard exists for (`1064759-0f23fb68`, seven sweeps at 79/101), the oracle drift
that makes a bare pass count ambiguous (`675043`→`669347` era, 39 read at oracles
79/87/101/103/105), and the healthy stub-paid climb that must NOT trip
(`7445-00ee8a3c`, 11 → 13 → 13 while stubs fell 132 → 118 → 82 → 75).

Every `observed`/`trips_on` here carries a TRAVERSAL index — how many complete
passes over the frontier the loop had finished when the reading was taken. The
uncapped loop advances it every iteration, which is what the `_t` counter in most
cases below models; the capped loop advances it once per (frontier/cap)
iterations, and the two cases at the end are that difference.
"""
from __future__ import annotations

from itertools import count

from pipeline_core.domain.flat_score import FlatScoreGuard, ScoreReading


def _guard(**kw: int) -> FlatScoreGuard:
    return FlatScoreGuard(**kw)  # type: ignore[arg-type]


def test_a_reading_seen_twice_in_the_window_trips() -> None:
    g = _guard()
    r79 = ScoreReading(79, 101)
    g = g.observed(r79, 0)
    assert not g.trips_on(r79, 0), "one reading is a measurement, not a repeat"
    g = g.observed(r79, 1)
    assert g.trips_on(r79, 1)


def test_the_repeat_need_not_be_consecutive() -> None:
    """The recorded pathology is a FIXED POINT, not a flat line: the score
    leaves 79 for the sweep after a re-stub and comes back to it."""
    g, t = _guard(), count()
    for r in (ScoreReading(79, 101), ScoreReading(66, 101)):
        g = g.observed(r, next(t))
    assert g.trips_on(ScoreReading(79, 101), 2) is False, "not yet recorded"
    g = g.observed(ScoreReading(79, 101), 2)
    assert g.trips_on(ScoreReading(79, 101), 2)


def test_only_the_newest_reading_can_trip() -> None:
    """A window holding a repeat is not a reason to stop a sweep that moved."""
    g, t = _guard(), count()
    for r in (ScoreReading(79, 101), ScoreReading(66, 101), ScoreReading(79, 101)):
        g = g.observed(r, next(t))
    assert not g.trips_on(ScoreReading(80, 101), 3)


def test_readings_leave_the_window() -> None:
    g, t = _guard(window=3, repeats=2), count()
    g = g.observed(ScoreReading(79, 101), next(t))
    for p in (60, 61, 62):
        g = g.observed(ScoreReading(p, 101), next(t))
    assert not g.trips_on(ScoreReading(79, 101), 4), \
        "a reading four sweeps old is outside a 3-sweep window"


def test_the_oracle_is_part_of_the_reading() -> None:
    """39/87 and 39/79 are different facts about the crate — the second oracle
    is eight tests smaller because a binary stopped reporting."""
    g = _guard()
    g = g.observed(ScoreReading(39, 87), 0)
    assert not g.trips_on(ScoreReading(39, 79), 1)
    g = g.observed(ScoreReading(39, 79), 1)
    assert not g.trips_on(ScoreReading(39, 87), 2), \
        "the pass count alone must never be the identity"


def test_an_excused_sweep_leaves_the_window_untouched() -> None:
    """A grace-window sweep measures a deliberate re-stub. It is not evidence
    in either direction: it neither trips the guard nor clears it, so a
    destruction whose whole re-climb lands back on the number it started from
    still stops."""
    g = _guard()
    g = g.observed(ScoreReading(79, 101), 0)
    for _ in range(4):
        g = g.excused()
    assert g.pending == 4
    assert not g.trips_on(ScoreReading(32, 105), 3), "the re-climb is not judged"
    g = g.observed(ScoreReading(79, 101), 3)
    assert g.trips_on(ScoreReading(79, 101), 3)
    assert (g.pending, g.gap) == (0, 4), \
        "the recording that trips must report how far apart the readings were"


def test_repeats_below_two_disables_the_guard() -> None:
    """A repeat count of 1 would fire on the first sweep, before any dispatch
    has been judged — that is not a tighter guard, it is a broken one."""
    for g in (_guard(repeats=1), _guard(repeats=0), _guard(window=1)):
        assert not g.enabled
        g2 = (g.observed(ScoreReading(79, 101), 0)
               .observed(ScoreReading(79, 101), 1))
        assert g2.readings == () and not g2.trips_on(ScoreReading(79, 101), 1)


def test_a_higher_limit_needs_more_repeats() -> None:
    g, t = _guard(window=5, repeats=3), count()
    r = ScoreReading(39, 87)
    g = g.observed(r, next(t)).observed(r, next(t))
    assert not g.trips_on(r, 2)
    g = g.observed(r, 2)
    assert g.trips_on(r, 2)


def test_a_repeat_inside_one_traversal_stops_the_run() -> None:
    """THE NEGATIVE CONTROL for the 2026-08-17 change, on the sequence that
    cost run `1920662-40f711b2` its night.

    That run read `83/106` at iterations 2, 3 and 4 without ever completing a
    frontier traversal: the cohort opened at iter 2 over a frontier of 43, iter
    3 attempted 15 of them against a frontier that had shrunk to 16, and iter 4
    left through a crash retrigger's `continue`, which commits no batch. The
    guard's own line printed at 00:25:43Z — `83/106 has been read 2 time(s)
    (limit 2) but all of them inside traversal 0` — and the run went on for
    another five hours.

    Injecting the pre-fix behaviour (`require_traversal=True`) reproduces that
    exactly, which is what makes this a control rather than a restatement."""
    g = _guard()
    r = ScoreReading(83, 106)
    g = g.observed(r, 0)
    assert not g.trips_on(r, 0), "one reading is a measurement, not a repeat"
    g = g.observed(r, 0)
    assert g.trips_on(r, 0), \
        "the same number twice IS the rule — no traversal precondition"

    old = _guard(require_traversal=True)  # type: ignore[arg-type]
    for _ in range(4):                    # four capped sweeps, one traversal
        old = old.observed(r, 0)
    assert old.repeated(r) == 4 >= old.repeats
    assert not old.trips_on(r, 0), \
        "pre-fix: four repeats inside traversal 0 did not stop the run"
    assert old.withheld_by_traversal(r, 0), \
        "pre-fix: armed and withheld — the state the run spent 9h48m in"


def test_the_withheld_report_is_vacuous_once_the_clause_is_off() -> None:
    """`withheld_by_traversal` is the negative of `trips_on`, so with the
    precondition off nothing is ever withheld. It survives as a method so that
    re-arming the clause restores its reporting in the same switch."""
    g = _guard()
    r = ScoreReading(83, 106)
    g = g.observed(r, 0).observed(r, 0)
    assert g.trips_on(r, 0)
    assert not g.withheld_by_traversal(r, 0)


def test_a_non_consecutive_repeat_trips_without_a_traversal() -> None:
    """The operator's rule, stated as a sequence: `83 -> 79 -> 83` is a fixed
    point and must stop just as `83 -> 83` does, and neither may wait on a
    completed pass. The window is what carries this; the middle reading is a
    re-climb, not progress."""
    g, r83, r79 = _guard(), ScoreReading(83, 106), ScoreReading(79, 106)
    g = g.observed(r83, 0).observed(r79, 0).observed(r83, 0)
    assert g.trips_on(r83, 0)


def test_the_clause_is_free_when_every_batch_closes_a_pass() -> None:
    """A frontier under the cap (and the cap disabled) completes a pass every
    iteration, so the index advances every sweep and the rule is exactly the
    two-readings rule it started as. That is why this clause is invisible in
    every walk — all of them drive a one-fn frontier."""
    g, t = _guard(), count()
    r = ScoreReading(39, 87)
    g = g.observed(r, next(t))
    assert not g.trips_on(r, 1)
    g = g.observed(r, next(t))
    assert g.trips_on(r, 1), "second sweep, second traversal, stop"


def test_render_names_the_window_the_traversal_and_the_count() -> None:
    g = (_guard().observed(ScoreReading(79, 101), 0)
         .observed(ScoreReading(66, 101), 1))
    line = g.render(ScoreReading(79, 101))
    assert "79/101@t0 66/101@t1" in line and "79/101 1x" in line
