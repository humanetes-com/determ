"""Epoch-stamped progress baselines: cross-epoch comparisons refuse."""
from __future__ import annotations

from pipeline_core.domain.destruction_epoch import (
    BoundaryDecision,
    EpochedBaseline,
    ProgressComparison,
    compare_progress,
    readmission_boundary,
)


def test_same_epoch_improvement() -> None:
    assert compare_progress(
        88, baseline=EpochedBaseline(81, 0), current_epoch=0,
    ) is ProgressComparison.IMPROVED


def test_same_epoch_no_improvement_includes_equal() -> None:
    for cur in (81, 21):
        assert compare_progress(
            cur, baseline=EpochedBaseline(81, 0), current_epoch=0,
        ) is ProgressComparison.NOT_IMPROVED


def test_cross_epoch_refuses_before_reading_the_value() -> None:
    # The re-stub deadlock's exact shape: baseline 81 from epoch 0, current
    # tree at 21 after a deliberate destruction. NOT_IMPROVED here is the
    # permanent lock; the only honest answer is INCOMPARABLE.
    assert compare_progress(
        21, baseline=EpochedBaseline(81, 0), current_epoch=1,
    ) is ProgressComparison.INCOMPARABLE
    # Even a value that WOULD improve is refused — the comparison is
    # meaningless in both directions, not just the harmful one.
    assert compare_progress(
        95, baseline=EpochedBaseline(81, 0), current_epoch=1,
    ) is ProgressComparison.INCOMPARABLE


def test_rebase_adopts_value_and_epoch() -> None:
    b = EpochedBaseline(81, 0).rebased(21, 1)
    assert b == EpochedBaseline(21, 1)
    assert compare_progress(22, baseline=b, current_epoch=1,
                            ) is ProgressComparison.IMPROVED


# --- readmission_boundary: order is the contract ------------------------------
# The numbers below are run `58819-ee78a1d1` (2026-08-13), iters 4-6: the
# retrigger re-stubbed 3 bodies after iter 3's 79-sweep and again after iter
# 5's, the tree measured 74 between, and the release iter 5's 74 -> 79
# re-climb earned was eaten because the epoch re-base at the PRE-sweep
# boundary adopted the pre-destruction 79 before the high-water accounting
# could correct it to 74.


def test_iter6_with_presweep_values_reproduces_the_eaten_release() -> None:
    """The negative control, on the recorded values. At the iter-6 boundary
    BEFORE the sweep, `best_pass` was still the pre-destruction 79 (iter 5's
    sweep; the fire came after it). Handed that number, the boundary re-bases
    74 -> 79 and then refuses against itself — the recorded outcome, and the
    reason the decision may only run with post-sweep values."""
    out = readmission_boundary(
        best_pass=79, baseline=EpochedBaseline(74, 1), destruction_epoch=2,
        new_fills=16, sweep_measured=True, sweep_reconciled=True)
    assert out.rebased_from == 74
    assert out.baseline == EpochedBaseline(79, 2)
    assert out.decision is BoundaryDecision.REFUSE


def test_iter6_with_postsweep_values_rebases_to_the_tree_on_disk() -> None:
    """With `best_pass` already carried through the sweep and the high-water's
    own destruction re-base (79 -> 74), the epoch re-base adopts the tree on
    disk: baseline 74, epoch current — and the invariant `readmission baseline
    <= high-water` holds where the pre-sweep ordering broke it (79 > 74)."""
    out = readmission_boundary(
        best_pass=74, baseline=EpochedBaseline(74, 1), destruction_epoch=2,
        new_fills=0, sweep_measured=True, sweep_reconciled=True)
    assert out.rebased_from is None          # value already right; epoch stamps
    assert out.baseline == EpochedBaseline(74, 2)
    assert out.decision is BoundaryDecision.NONE


def test_a_reclimb_past_the_rebased_baseline_releases() -> None:
    """The decision the fix exists to make reachable: iter 5's sweep measured
    79 against the correctly re-based 74, and with the decision AFTER the
    sweep that is a release — the same values at the pre-sweep boundary read
    best=74 (the 79 not yet measured) and refused."""
    out = readmission_boundary(
        best_pass=79, baseline=EpochedBaseline(74, 1), destruction_epoch=1,
        new_fills=16, sweep_measured=True, sweep_reconciled=True)
    assert out.decision is BoundaryDecision.RELEASE
    assert out.baseline == EpochedBaseline(79, 1)


def test_a_compile_broken_sweep_defers_instead_of_rebasing() -> None:
    """A sweep that measured nothing can neither re-base a
    destruction-invalidated baseline nor justify a decision against it."""
    out = readmission_boundary(
        best_pass=79, baseline=EpochedBaseline(74, 1), destruction_epoch=2,
        new_fills=16, sweep_measured=False, sweep_reconciled=True)
    assert out.decision is BoundaryDecision.DEFER_UNMEASURED
    assert out.baseline == EpochedBaseline(74, 1)   # untouched, still stale
    assert out.rebased_from is None


def test_an_unspent_free_grant_survives_the_epoch_stamp() -> None:
    """The negative sentinel is not a baseline yet; re-basing it would spend
    the deadlock-breaker grant on bookkeeping. The epoch still stamps."""
    out = readmission_boundary(
        best_pass=67, baseline=EpochedBaseline(-1, 0), destruction_epoch=1,
        new_fills=0, sweep_measured=True, sweep_reconciled=True)
    assert out.baseline == EpochedBaseline(-1, 1)
    assert out.rebased_from is None


def test_a_tainted_sweep_defers_after_rebasing() -> None:
    """The re-base is bookkeeping and proceeds; the DECISION is what a
    disclaimed score cannot justify."""
    out = readmission_boundary(
        best_pass=74, baseline=EpochedBaseline(79, 1), destruction_epoch=2,
        new_fills=16, sweep_measured=True, sweep_reconciled=False)
    assert out.decision is BoundaryDecision.DEFER_TAINTED
    assert out.baseline == EpochedBaseline(74, 2)
    assert out.rebased_from == 79
