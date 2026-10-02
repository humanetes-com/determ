"""A refused splice as an attempt outcome — the one rule (pure domain).

The splicer refuses a write for two kinds of reason. Some are about the tree
or the route: the target is already filled, the fn is not in this cluster's
cfg world, the record no longer locates. No reply can change those, and the
unit stops. The others are a verdict on the REPLY'S TEXT: it does not parse,
it closed its fn early and declared items at file scope, it restated the fn
it was asked to fill. Those are the failures a second turn repairs most
cheaply, and until 2026-10-02 they ended the unit like the first kind: the
model was never told, the attempt was not charged, and the retrospective read
the untouched tree as "compiles, 0 failing tests, improved".

Run `473471-33301154` (2026-09-29, cluster `stable`): `object_ref`, the stub
60 of 141 failing tests stopped in, was answered with a body whose last line
cast to `&dyn StdError + Send + Sync + 'static`. rustc's parser asks for
parentheses there. The splice was refused `parse_broken`, the unit was
reverted after 1 attempt, and every other keep of that fill logged `blocked
by still-stubbed sibling(s)` naming it.

:func:`reply_refusal_feedback` is the rule. It returns the text an attempt's
error slot carries for a refusal of the second kind, and None for the first.
Which reasons are which is this module's set, not a flag a caller builds, so
a route cannot turn a tree-side refusal into a re-ask or drop a reply-side
one by its own reading.
"""
from __future__ import annotations

from pipeline_core.domain.reconstruction import SpliceOutcome

# Refusals that judge the text of the reply. Everything else in
# `SpliceReason` judges the tree, the record or the route.
_REPLY_RULES: dict[str, str] = {
    "parse_broken": (
        "the body does not parse as Rust once placed between the function's "
        "braces, so it was not written"),
    "item_set_changed": (
        "the body closes the function early and declares items at file "
        "scope. Write ONLY what goes between the function's braces: no "
        "closing `}` of the function itself, no new `fn`/`impl`/`struct` "
        "after it. It was not written"),
    "body_redeclares_fn": (
        "the body restates the function's own `fn …` line inside itself. "
        "Write ONLY what goes between the braces. It was not written"),
}

REPLY_REFUSALS: frozenset[str] = frozenset(_REPLY_RULES)


def reply_refusal_feedback(
    outcome: SpliceOutcome, *, unit: str, file_rel: str = "",
) -> str | None:
    """What the next turn is told about a refused splice of `unit`, or None
    when the refusal is not about the reply's text.

    Shaped as a compiler error, because that is the slot it travels in and
    what it is: the parser's own diagnostic when the refusal carries one,
    with its location named as the unit's file."""
    if outcome.ok or outcome.reason not in _REPLY_RULES:
        return None
    head = f"error: your body for `{unit}` was refused: {_REPLY_RULES[outcome.reason]}."
    diagnostic = str(getattr(outcome, "diagnostic", "") or "").strip()
    if not diagnostic:
        return head
    if file_rel:
        diagnostic = diagnostic.replace("<stdin>", file_rel)
    return f"{head}\nThe parser's diagnostic on the file as it would have been:\n{diagnostic}"
