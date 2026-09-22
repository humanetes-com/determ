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
    """Any single axis moving is payoff: score, crash class, or the per-binary
    verdict vector."""
    assert paid_off(1, False, False)
    assert paid_off(0, True, False)
    assert paid_off(0, False, True)
    assert not paid_off(0, False, False)
    # A score DROP alone is not payoff — a deliberate destruction's drop is
    # excluded upstream (grace sweeps are never recorded), and an undeliberate
    # drop with nothing else moving is the pathology, not progress.
    assert not paid_off(-5, False, False)


def test_a_moving_frontier_is_not_payoff_by_itself():
    """The counterexample the axis was changed for. Run `1064759-0f23fb68`
    (2026-08-15/16) took its frontier 60 -> 59 -> 59 -> 30 -> 30 while six of
    its seven sweeps carried a byte-identical per-binary vector, so every sweep
    scored as PAYING and the detector fired 0 times across the whole 7h48m.
    The frontier shrinks when units are judged resolved, which is bookkeeping
    about the loop's plan, not evidence about the crate."""
    assert not paid_off(0, False, False, True)


def test_the_frontier_counts_only_across_the_loops_own_destruction():
    """The one place the plan legitimately moves for a reason the loop caused:
    a retrigger re-stubbed bodies between the two readings, so the frontier
    HAD to change and its movement is not re-planning."""
    assert paid_off(0, False, False, True, restub_between=True)
    assert not paid_off(0, False, False, False, restub_between=True)


def test_the_binary_vector_catches_what_the_score_hides():
    """A cluster whose score is flat because one test flipped each way has
    moved; the pass count alone cannot see it, and a producer chain closing
    shows up here long before it shows up in the score."""
    assert paid_off(0, False, True)


def test_the_unpaid_streak_fires_on_a_rotating_aim():
    """The rule a BATCHED loop needs. Run `1064759-0f23fb68` dispatched 12 of
    59, 12 of 59, then 10 of 30 with a byte-identical per-binary vector
    throughout: nothing was bought for five sweeps and no two of them aimed at
    the same set, so the identity rule cannot fire."""
    from pipeline_core.domain.zero_payoff import unpaid_streak
    rotating = (_o(A, False), _o(B, False), _o(frozenset({"x"}), False))
    assert zero_payoff_streak(rotating) is None, "the aims differ"
    assert unpaid_streak(rotating) == 3


def test_the_unpaid_streak_resets_on_any_payoff():
    from pipeline_core.domain.zero_payoff import unpaid_streak
    assert unpaid_streak((_o(A, False), _o(A, True), _o(A, False))) is None
    assert unpaid_streak((_o(A, False), _o(A, False)), window=2) == 2
    assert unpaid_streak((_o(A, False), _o(A, False))) is None, \
        "two unpaid sweeps do not reach the default window of three"


def test_a_closing_stub_chain_pays_on_its_moved_failure_sites():
    """Run `497151-a6fa205b` (2026-09-22): three sweeps with an identical
    per-binary vector and a flat pass count, each with 92–100 failing tests
    now failing one stub further down their path. That is a chain closing,
    the healthy build the shadow period exists to tell apart; the detector
    fired on it at iter 4. Negative control: the same sweeps without the
    site axis are an unpaid streak."""
    from pipeline_core.domain.zero_payoff import unpaid_streak
    closing = tuple(
        _o(t, paid_off(0, False, False, True, sites_moved=True))
        for t in (A, B, frozenset({"x"})))
    assert unpaid_streak(closing) is None
    blind = tuple(_o(t, paid_off(0, False, False, True))
                  for t in (A, B, frozenset({"x"})))
    assert unpaid_streak(blind) == 3
