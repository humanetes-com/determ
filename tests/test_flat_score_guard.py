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


def test_two_readings_inside_one_traversal_are_two_slices_of_one_plan() -> None:
    """THE CLAUSE THE DISPATCH CAP MADE NECESSARY, and the reason this file
    carries a traversal index at all.

    Under `PHASE_C_TDD_MAX_DISPATCH=12` a sweep measures 12 units of a 59-unit
    frontier, so "79/101 twice running" says twelve units bought nothing — not
    that the frontier did. Stopping there stops the run having attempted 24 of
    59, and it stops there again on every relaunch, because `order_frontier` is
    deterministic and a new process starts with an empty window and an empty
    attempted-set: the same first 12 go out and read the same score. The
    remaining 35 units are then unreachable without a human editing the env.
    """
    g = _guard()
    r = ScoreReading(79, 101)
    for _ in range(4):                      # four capped sweeps, one traversal
        g = g.observed(r, 0)
    assert g.repeated(r) == 4 >= g.repeats
    assert not g.trips_on(r, 0), \
        "four slices of ONE plan is not evidence about the plan"
    g = g.observed(r, 1)                    # the traversal closed; same reading
    assert g.trips_on(r, 1), \
        "a repeat that survives the loop finishing its plan is the evidence"


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
