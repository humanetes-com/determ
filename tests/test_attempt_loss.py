"""Pins for `domain.attempt_loss` — the budget-off escalation guard.

Six maximal last-resort attempts in run `2879-66905005` spent ≈193 k prompt
chars, and at least one of them (`chain.rs::DefaultforChain<>`) fired because
the pipeline had thrown a correct answer away over a one-character canon
mismatch. A bigger prompt cannot address a loss the model did not cause.
"""
from __future__ import annotations

from pipeline_core.domain.attempt_loss import (
    AttemptLoss,
    escalation_refusal_note,
    may_escalate_to_last_resort,
)


def test_a_model_failure_still_escalates() -> None:
    """The case the last-resort exists for: the reply was the loss, and the
    defect may well be outside the budgeted window."""
    assert may_escalate_to_last_resort(AttemptLoss.MODEL_FAILURE)
    assert not AttemptLoss.MODEL_FAILURE.is_pipeline_side


def test_pipeline_side_losses_do_not_escalate() -> None:
    for loss in (AttemptLoss.ROUTING_DROP, AttemptLoss.SPLICE_REFUSAL):
        assert loss.is_pipeline_side
        assert not may_escalate_to_last_resort(loss)


def test_no_recorded_loss_escalates() -> None:
    """The pre-guard state. A guard must only ever subtract the case it was
    written for — never withhold on an unknown."""
    assert may_escalate_to_last_resort(None)


def test_the_refusal_says_which_side_lost_the_answer() -> None:
    note = escalation_refusal_note(AttemptLoss.ROUTING_DROP)
    assert "routing_drop" in note
    assert "dropped" in note


def test_losses_are_their_wire_strings() -> None:
    """StrEnum, like `FailureKind` — the event payload carries the member."""
    assert AttemptLoss.ROUTING_DROP == "routing_drop"
    assert f"{AttemptLoss.SPLICE_REFUSAL}" == "splice_refusal"


def test_unchanged_signature_withholds_whatever_the_loss() -> None:
    """The second provably wasted escalation: identical error signature
    across the budgeted attempts. `src/error.rs::ErrorImpl` fired 19
    maximal prompts life-to-date on a `7 failing` signature that never
    moved across three consecutive runs (audited 2026-08-10)."""
    from pipeline_core.domain.attempt_loss import invariance_refusal_note
    for loss in (None, AttemptLoss.MODEL_FAILURE, AttemptLoss.ROUTING_DROP):
        assert not may_escalate_to_last_resort(loss, signature_unchanged=True)
    note = invariance_refusal_note(3)
    assert "3 attempt(s)" in note
    assert "identical" in note


def test_changed_signature_leaves_the_old_rule_intact() -> None:
    assert may_escalate_to_last_resort(None, signature_unchanged=False)
    assert may_escalate_to_last_resort(AttemptLoss.MODEL_FAILURE,
                                       signature_unchanged=False)
    assert not may_escalate_to_last_resort(AttemptLoss.SPLICE_REFUSAL,
                                           signature_unchanged=False)
