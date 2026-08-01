"""Pins for `domain.pass_payoff` — a pass that fills and cannot score.

The measured case, run `2879-66905005`: 71 units, 40 landed bodies, ~2.7 h and
the bulk of 3.3 M prompt tokens, and every attributed test panicked inside the
same still-stubbed `error.rs::construct`. `progress_this_pass` read 40, so the
loop's only stop condition ("did anything fill?") never fired.
"""
from __future__ import annotations

from pipeline_core.domain.models import FillResult, FnSite
from pipeline_core.domain.pass_payoff import (
    UnitOutcome,
    assess_fill_results,
    assess_pass_payoff,
)

CONSTRUCT = FnSite("src/error.rs", "construct", "impl Error")
SOURCE = FnSite("src/error.rs", "source", "impl StdError for Error")


def test_one_blocker_common_to_every_landed_body_is_payoff_zero() -> None:
    payoff = assess_pass_payoff([
        UnitOutcome(landed=True, blocked_by=(CONSTRUCT,)),
        UnitOutcome(landed=True, blocked_by=(CONSTRUCT, SOURCE)),
    ])
    assert payoff.is_zero
    assert payoff.blockers == frozenset({CONSTRUCT})
    assert payoff.landed == 2
    assert "construct" in payoff.summary


def test_a_judgeable_fill_disproves_payoff_zero() -> None:
    """One unit judged on its own merits means a verdict WAS reachable —
    whatever the others were blocked by."""
    payoff = assess_pass_payoff([
        UnitOutcome(landed=True, blocked_by=(CONSTRUCT,)),
        UnitOutcome(landed=True),
    ])
    assert not payoff.is_zero
    assert payoff.blockers == frozenset()


def test_disjoint_blockers_are_not_payoff_zero() -> None:
    """Units blocked by DIFFERENT siblings can still each be unblocked by
    separate work; nothing here proves the pass could not score. The rule is
    an intersection precisely so this case is excluded."""
    payoff = assess_pass_payoff([
        UnitOutcome(landed=True, blocked_by=(CONSTRUCT,)),
        UnitOutcome(landed=True, blocked_by=(SOURCE,)),
    ])
    assert not payoff.is_zero


def test_a_pass_that_landed_nothing_is_not_payoff_zero() -> None:
    """The ordinary no-progress case, which the loop already stops on. Calling
    it payoff-zero would blame a blocker for an empty pass."""
    assert not assess_pass_payoff([]).is_zero
    assert not assess_pass_payoff([UnitOutcome(landed=False)]).is_zero
    payoff = assess_pass_payoff([UnitOutcome(landed=False, blocked_by=(CONSTRUCT,))])
    assert not payoff.is_zero
    assert payoff.landed == 0


def test_a_single_blocked_unit_is_enough_when_it_is_the_whole_pass() -> None:
    payoff = assess_pass_payoff([UnitOutcome(landed=True, blocked_by=(CONSTRUCT,))])
    assert payoff.is_zero
    assert payoff.blockers == frozenset({CONSTRUCT})


def test_reads_the_per_fn_routes_result_type() -> None:
    payoff = assess_fill_results([
        FillResult(file="src/ensure.rs", fn_name="not", filled=True,
                   blocked_by=(CONSTRUCT,)),
        FillResult(file="src/chain.rs", fn_name="len", filled=True,
                   blocked_by=(CONSTRUCT,)),
        FillResult(file="src/fmt.rs", fn_name="fmt", filled=False,
                   error="exhausted"),
    ])
    assert payoff.is_zero
    assert payoff.landed == 2
    assert payoff.blockers == frozenset({CONSTRUCT})


def test_fill_result_defaults_to_unblocked() -> None:
    """The field is additive: every existing construction site keeps meaning
    "judged on its own merits", which is what those sites always meant."""
    assert FillResult(file="a.rs", fn_name="f", filled=True).blocked_by == ()
    assert not assess_fill_results(
        [FillResult(file="a.rs", fn_name="f", filled=True)]).is_zero
