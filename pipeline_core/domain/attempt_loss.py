"""Why an attempt landed nothing — and whether that justifies escalating.

The per-impl fill loop ends a failed round with ONE budget-off "maximal
last-resort" attempt: the whole file, every referencing test verbatim, the full
error. It is by far the most expensive call the pipeline makes — six of them in
run `2879-66905005` cost 53 099, 47 685, 33 281, 28 708, 18 559 and 11 716
chars, ≈193 k in a single run.

It is the right answer to exactly one kind of loss: the model was shown too
little. A defect in a sibling member, or a type defined elsewhere in the file,
is invisible through the budgeted window, and widening the window is the fix.

It is the wrong answer to the other kind. When the model ANSWERED and the
pipeline discarded the answer — the body's block header matched no container
(`routing_drop`), or the splice refused the write — nothing about the prompt was
too small. At least one of those six escalations (`chain.rs::DefaultforChain<>`)
fired because `canon_container` threw a correct answer away over a missing `>`.
The loop charged the model for the pipeline's own drop, and the largest prompts
in the run went out on the paths least able to use them.

`StarvationCause.RESPONSE_UNUSED` is the same observation made after the fact by
the starvation miner. This module is that judgement available to the loop while
it is still deciding, so it can re-route or re-ask instead of escalating.
"""

from __future__ import annotations

from enum import StrEnum


class AttemptLoss(StrEnum):
    """The closed set of reasons one fill attempt produced no landed body."""

    MODEL_FAILURE = "model_failure"
    """The reply itself was the loss: empty, no `<FN>` blocks, or a body that
    did land and failed to compile or to pass its own tests. The default — any
    loss not positively identified as pipeline-side is the model's, which keeps
    the escalation available wherever it was available before."""

    ROUTING_DROP = "routing_drop"
    """The model answered and every body was discarded before reaching the
    file: no block header matched the target or any related container."""

    SPLICE_REFUSAL = "splice_refusal"
    """The model answered and the write was refused (guard, non-stub target,
    cfg scope). Same shape as a routing drop: an answer exists, unused."""

    @property
    def is_pipeline_side(self) -> bool:
        """True when the answer was lost after the model produced it.

        The distinction that matters for spending: a pipeline-side loss is not
        evidence about the model at all, so no amount of extra context addresses
        it."""
        return self in _PIPELINE_SIDE


_PIPELINE_SIDE = frozenset({AttemptLoss.ROUTING_DROP, AttemptLoss.SPLICE_REFUSAL})


def may_escalate_to_last_resort(loss: AttemptLoss | None) -> bool:
    """Whether a budget-off retry is warranted after this loss.

    `None` — no attempt recorded a loss — permits it: that is the state before
    this tracking existed, and the guard must only ever subtract the case it
    was written for."""
    return loss is None or not loss.is_pipeline_side


def escalation_refusal_note(loss: AttemptLoss) -> str:
    """Why the escalation was withheld, for the log line that replaces it."""
    return (f"last loss was {loss.value} — the model answered and the answer "
            "was dropped before it reached the file; a bigger prompt cannot "
            "fix a routing/splice loss")
