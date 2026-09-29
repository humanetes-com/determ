"""The two prompts that build the behaviour book's rules at t0d.

AUTHOR: one feature's checkable examples (their helpers, setup, when and the
recorded Then), the item's glossary lines and the crate's vocabulary → one-
sentence rules, each citing the example ids it covers.

PREDICT: examples WITHOUT their Then, optionally with one rule sentence → the
Then of each. Run once per feature with no rule (the ablation) and once per
drafted rule over the examples it cites; `behaviour_book.judge_draft` keeps a
rule only when the second predicts every cited Then and the first misses one.

Legality (Hard Rule 11): the inputs are the crate's tests (the oracle) and the
first sentence of each PUBLIC item's doc comment, read pre-strip at t0d. No
body, no private name — a sentence that names a private item is dropped by
`draft_refusal` before it is ever checked.
"""
from __future__ import annotations

from collections.abc import Sequence

from pipeline_core.domain.behaviour import BehaviourSpec, GlossaryEntry, OutcomeKind, example_when, then_value_text

_QUESTION = {
    OutcomeKind.DISPLAY_STRING: "its Display text (`to_string()` / `{}`), exactly",
    OutcomeKind.DEBUG_STRING: 'its Debug text (`format!("{:?}")`), exactly',
    OutcomeKind.ALT_DEBUG_STRING: 'its alternate Debug text (`format!("{:#?}")`), exactly',
    OutcomeKind.RETURN_RENDERED: "the value it evaluates to, as the test compares it",
    OutcomeKind.PANIC_MESSAGE: "the panic message",
    OutcomeKind.NORMALIZED_STDOUT: "what it prints",
    OutcomeKind.CHAIN_SEQUENCE: "each element's Display text, one per line, in order",
    OutcomeKind.TYPE_IDENTITY: "`true` or `false`: whether the type check holds",
    OutcomeKind.DOWNCAST_OK: "`true` or `false`: whether the downcast finds a value",
}


def _question(spec: BehaviourSpec) -> str:
    assert spec.then is not None
    kind = spec.then.kind
    if kind is OutcomeKind.TYPE_IDENTITY:
        return f"`true` or `false`: whether it is a `{spec.then.value}` (`is::<{spec.then.value}>()`)"
    q = _QUESTION.get(kind, "the observable, exactly")
    if spec.via:
        return f"the text `{spec.via}` compares with its literal argument — {q}"
    return q


def _example_block(spec: BehaviourSpec, *, with_then: bool) -> str:
    parts = [f'<EXAMPLE id="{spec.behaviour_id}">']
    context = "\n".join(p for p in (*spec.helpers, spec.setup) if p.strip())
    if context:
        parts.append("GIVEN:\n```rust\n" + context + "\n```")
    parts.append("WHEN:\n```rust\n" + example_when(spec) + "\n```")
    parts.append("ASKED: " + _question(spec))
    if with_then and spec.then is not None:
        value = then_value_text(spec.then)
        prefix = " (the text STARTS WITH this; the rest varies by run)" if spec.then.prefix else ""
        parts.append(f"THEN{prefix}:\n<VALUE>{value}</VALUE>")
    parts.append("</EXAMPLE>")
    return "\n".join(parts)


def glossary_lines(glossary: Sequence[GlossaryEntry]) -> str:
    return "\n".join(f"- `{g.name}` ({g.kind})" + (f": {g.summary}" if g.summary else "")
                     for g in glossary)


def build_rule_authoring_prompt(
    *,
    crate: str,
    feature: str,
    feature_lines: Sequence[GlossaryEntry],
    glossary: Sequence[GlossaryEntry],
    examples: Sequence[BehaviourSpec],
) -> str:
    """Ask for the invariants `examples` illustrate, as rules."""
    item = "\n".join(f"- `{g.name}`" + (f": {g.summary}" if g.summary else "") for g in feature_lines) \
        or f"- `{feature}`"
    blocks = "\n\n".join(_example_block(s, with_then=True) for s in examples)
    return f"""You are writing the specification of the Rust crate `{crate}` from its own tests.

FEATURE (the public item these examples exercise):
{item}

VOCABULARY (the crate's public names; write rules in these terms):
{glossary_lines(glossary)}

EXAMPLES (each: the code, what was asked of it, and the value the crate produced):

{blocks}

TASK: state the observable RULES these examples illustrate. A rule is ONE
sentence a user of the crate could rely on, general enough to predict every
example it cites, including ones not listed. It describes what the public API
does, never how: do not name any item that is not in the VOCABULARY or in the
examples' own code, and do not quote an example's value — state the pattern
that produces it (for instance which parts of the input appear in the output,
in what order, with what separators).

Cover every example with some rule; one rule may cite many examples. Answer
with one block per rule and nothing else:

<RULE covers="<example id>,<example id>">The sentence.</RULE>
"""


def build_then_prediction_prompt(
    *,
    crate: str,
    glossary: Sequence[GlossaryEntry],
    examples: Sequence[BehaviourSpec],
    rule: str = "",
) -> str:
    """Ask for the Then of each example; with `rule`, the one sentence the
    predictor may rely on."""
    blocks = "\n\n".join(_example_block(s, with_then=False) for s in examples)
    rule_part = (f"\nRULE (holds for every example below; rely on it):\n{rule}\n" if rule else "")
    return f"""Predict what the Rust crate `{crate}` produces for each example.

VOCABULARY (the crate's public names):
{glossary_lines(glossary)}
{rule_part}
{blocks}

For EACH example answer the ASKED value exactly — no quotes around it unless
they are part of the value, no explanation — as:

<THEN id="<example id>">the value</THEN>
"""
