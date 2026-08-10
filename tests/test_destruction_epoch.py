"""Epoch-stamped progress baselines: cross-epoch comparisons refuse."""
from __future__ import annotations

from pipeline_core.domain.destruction_epoch import (
    EpochedBaseline,
    ProgressComparison,
    compare_progress,
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
