"""The two prompts that build the behaviour book's rules at t0d.

AUTHOR: one feature's checkable examples (their helpers, setup, when and the
recorded Then), the item's glossary lines and the crate's vocabulary → one-
sentence rules, each citing the example ids it covers.

PREDICT: examples WITHOUT their Then, optionally with a set of rule sentences
→ the Then of each. `behaviour_book.judge_rule_set` runs it per chunk with no
rule (the ablation), with every surviving draft (the set), and with the set
minus one draft over the examples that draft cites (leave-one-out); a rule is
kept for the examples the set gains and the set without it loses.

GIVEN PHRASES: one test file's roots (the test-file items its examples are
given, with the items those pull in) → one sentence per root, for the
generated view. The reply is read by nothing but the view.

Legality (Hard Rule 11): the inputs are the crate's tests (the oracle) and the
first sentence of each PUBLIC item's doc comment, read pre-strip at t0d. No
body, no private name — a sentence that names a private item is dropped by
`draft_refusal` before it is ever checked.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    GlossaryEntry,
    OutcomeKind,
    example_given,
    example_when,
    helper_text,
    then_value_text,
)
from pipeline_core.domain.behaviour_book import GivenRoot

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


def _example_block(spec: BehaviourSpec, *, with_then: bool,
                   worlds: Mapping[str, Sequence[str]] | None = None) -> str:
    parts = [f'<EXAMPLE id="{spec.behaviour_id}">']
    only = (worlds or {}).get(spec.behaviour_id, ())
    if only:
        parts.append("BUILT ONLY IN: " + ", ".join(only))
    # Every part of the example's given, through the one accessor: the
    # asserting helper and the `use` block are sent with the roots because
    # the judge was measured on this text.
    given = example_given(spec)
    context = "\n".join(p for p in (*helper_text(spec, given.keys), given.setup) if p.strip())
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


def _entry_kind(g: GlossaryEntry) -> str:
    return f"{g.kind}; only in the {', '.join(g.worlds)} build" if g.worlds else g.kind


def glossary_lines(glossary: Sequence[GlossaryEntry]) -> str:
    """One line per public name, a fn with its declaration. A name only some
    build worlds compile says which, and the list then opens with what that
    mark means: the book is drafted once over every world's vocabulary."""
    lines = [f"- `{g.name}` ({_entry_kind(g)})"
             + (f" `{g.signature}`" if g.signature else "")
             + (f": {g.summary}" if g.summary else "")
             for g in glossary]
    if any(g.worlds for g in glossary):
        lines.insert(0, "(The crate is built in more than one configuration. A name marked "
                        "`only in the X build` exists only there; every other name exists "
                        "in every build.)")
    return "\n".join(lines)


def build_rule_authoring_prompt(
    *,
    crate: str,
    feature: str,
    feature_lines: Sequence[GlossaryEntry],
    glossary: Sequence[GlossaryEntry],
    examples: Sequence[BehaviourSpec],
    worlds: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Ask for the invariants `examples` illustrate, as rules. `worlds`
    marks, by example id, an example only some build worlds compile."""
    item = "\n".join(f"- `{g.name}`" + (f" ({_entry_kind(g)})" if g.worlds else "")
                     + (f": {g.summary}" if g.summary else "") for g in feature_lines) \
        or f"- `{feature}`"
    blocks = "\n\n".join(_example_block(s, with_then=True, worlds=worlds) for s in examples)
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
    rules: Sequence[str] = (),
    worlds: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Ask for the Then of each example; with `rules`, the sentences the
    predictor may rely on, numbered, all at once — a rule may state part of
    an observable another rule completes."""
    blocks = "\n\n".join(_example_block(s, with_then=False, worlds=worlds) for s in examples)
    numbered = "\n".join(f"{n}. {r}" for n, r in enumerate(rules, 1))
    rule_part = (f"\nRULES (hold together for the examples below; rely on them):\n{numbered}\n"
                 if rules else "")
    return f"""Predict what the Rust crate `{crate}` produces for each example.

VOCABULARY (the crate's public names):
{glossary_lines(glossary)}
{rule_part}
{blocks}

For EACH example answer the ASKED value exactly — no quotes around it unless
they are part of the value, no explanation — as:

<THEN id="<example id>">the value</THEN>
"""


def build_given_phrase_prompt(*, crate: str, file: str, roots: Sequence[GivenRoot]) -> str:
    """Ask for one sentence per root of one test file. The input is test
    code only: each root's text and the test-file items it pulls in."""
    blocks = "\n\n".join(f'<FIXTURE key="{r.key}">\n```rust\n{r.text}\n```\n</FIXTURE>'
                         for r in roots)
    return f"""You are describing the fixtures the tests of the Rust crate `{crate}` build their cases on.

TEST FILE: {file}

FIXTURES (each: an item of the test files, followed by the test-file items it uses):

{blocks}

TASK: for EACH fixture write ONE sentence saying what a test that uses it is given: the
value it builds or the type it declares, and what is particular about it (what it wraps,
how many layers, what it returns, what it records). Say only what this code shows. Do not
say what the crate does with it and do not quote an expected output.

Answer with one block per fixture and nothing else:

<GIVEN key="<the fixture's key, exactly as given>">The sentence.</GIVEN>
"""


def build_rule_probe_prompt(
    *,
    crate: str,
    cfg_cluster: str,
    sentence: str,
    glossary: Sequence[GlossaryEntry],
    shown: Sequence[BehaviourSpec],
    near: Sequence[BehaviourSpec] = (),
    programs: int = 3,
) -> str:
    """Ask for programs a kept RULE covers that are not its examples
    (`domain/rule_probe.py`). No value is asked for and none is shown: the
    examples appear as code only, and the harness prints what the crate
    produces. The reply is the probe block shape, read by the probe parser.
    `near` are examples the rule was found wrong on."""
    covered = "\n\n".join(_example_block(s, with_then=False) for s in shown)
    near_part = ("\nThe rule was found WRONG on these cases. Write some of your programs close to "
                 "them (the same kind of input, varied):\n\n"
                 + "\n\n".join(_example_block(s, with_then=False) for s in near) + "\n"
                 if near else "")
    return f"""Test a rule of the Rust crate `{crate}` on new cases, through its PUBLIC API.

Crate: {crate} | Cluster: {cfg_cluster}

RULE (a sentence a user of the crate is told to rely on):
{sentence}

VOCABULARY (the crate's public names):
{glossary_lines(glossary)}

CASES THE RULE ALREADY COVERS (code only; do not repeat them):

{covered}
{near_part}
TASK: write {programs} programs the rule ALSO covers. Each must differ from every case
above in an input the rule's sentence speaks about (another value, another type, another
way of building it, a nested or empty or longer one). You do not know what the crate
prints: never write a value down; the harness runs your program and records it.

Output one block per program, EXACTLY this form and nothing else:

  <BEHAVIOUR name="snake_case_name">
  <GIVEN>
  // zero or more Rust statements building the values; no printing, no asserting
  </GIVEN>
  <WHEN>a SINGLE Rust expression</WHEN>
  <OBSERVE>display|debug|alt_debug</OBSERVE>
  </BEHAVIOUR>

Rules:
- PUBLIC API ONLY: the names in the VOCABULARY and the standard library.
- <WHEN> must evaluate to a RENDERED VALUE, never to a comparison (`==`, `!=`, `<=`,
  `>=`, `.contains(` are REJECTED). The ONLY booleans allowed are type-identity
  predicates: `x.is::<T>()` or `x.downcast_ref::<T>().is_some()`.
- <WHEN> is ONE expression; <GIVEN> may not print and may not assert.
- `{crate}`'s public items are ALREADY imported (`use {crate}::*;`). Any other import
  goes at the top of <GIVEN> as a normal `use` line.
"""
