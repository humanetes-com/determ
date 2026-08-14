"""Domain pins for the shadow zero-payoff detector (domain/zero_payoff.py).

The condition it detects is the class-general stall the 2026-08-14 census
measured: the same frontier dispatched K sweeps with nothing bought on any
axis.  The negative controls mirror the census's healthy runs: a rotating
frontier, a moving crash class, or a single paying sweep must all hold fire —
a detector that fires on a producer-chain build converts patience into panic.
"""

from pipeline_core.domain.zero_payoff import (
    SweepOutcome,
    paid_off,
    zero_payoff_streak,
)

A = frozenset({"src/a.rs::f", "src/a.rs::g"})
B = frozenset({"src/b.rs::h"})


def _o(targets, paid):
    return SweepOutcome(targets=targets, paid=paid)


def test_three_identical_unpaid_sweeps_fire():
    hist = (_o(A, False), _o(A, False), _o(A, False))
    assert zero_payoff_streak(hist) == A


def test_a_paying_sweep_inside_the_window_holds_fire():
    hist = (_o(A, False), _o(A, True), _o(A, False))
    assert zero_payoff_streak(hist) is None


def test_a_rotating_frontier_holds_fire():
    """A changed plan is the loop re-planning — payoff on the frontier axis
    by construction, never a streak."""
    hist = (_o(A, False), _o(B, False), _o(A, False))
    assert zero_payoff_streak(hist) is None


def test_short_history_and_empty_targets_hold_fire():
    assert zero_payoff_streak((_o(A, False), _o(A, False))) is None
    empty = frozenset()
    hist = (_o(empty, False), _o(empty, False), _o(empty, False))
    assert zero_payoff_streak(hist) is None


def test_the_streak_is_judged_on_the_tail():
    """Old paid sweeps do not immunize: the last three identical unpaid
    sweeps fire regardless of history before them."""
    hist = (_o(A, True), _o(B, True), _o(A, False), _o(A, False), _o(A, False))
    assert zero_payoff_streak(hist) == A


def test_payoff_axes():
    """Any single axis moving is payoff: score, crash class, or frontier."""
    assert paid_off(1, False, False)
    assert paid_off(0, True, False)
    assert paid_off(0, False, True)
    assert not paid_off(0, False, False)
    # A score DROP alone is not payoff — a deliberate destruction's drop is
    # excluded upstream (grace sweeps are never recorded), and an undeliberate
    # drop with nothing else moving is the pathology, not progress.
    assert not paid_off(-5, False, False)
