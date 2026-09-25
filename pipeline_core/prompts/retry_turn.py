"""The turn a fill conversation sends after a reply that was not kept.

Every fill route used to answer a failed attempt by building a NEW prompt —
the whole file, the tests, the types again, with the failure pasted in and
the previous body quoted back (per-fn `<PRIOR_ATTEMPTS>`, per-impl
`<LAST_ERROR>`, file-first `prior_error`) — and sending it to a model that
had never seen its own reasoning. The model's reply is now in the
conversation (`application.reconstruct.unit_conversation`), so what the next
turn has to carry is only what the harness MEASURED about that reply and
what has changed since: the failure and its kind, the sections the failure
newly makes relevant, the bodies other fills rewrote on disk, and the
output contract the route parses.

One rule decides what an evidence section adds: it is sent only when its
text is not already in the conversation. Types re-selected from the new
error, notes re-filtered by it, a diagnosis written after the last round —
each is new evidence exactly when the conversation does not hold it, and
repeating a section the model has above is the prompt growth the transcript
exists to avoid.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pipeline_core.domain.joint_write import JOINT_OUTPUT_CLAUSE
from pipeline_core.domain.last_error import (
    LastErrorKind,
    instruction_for,
    last_error_prefix,
    resolve_kind,
)
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.prompt_holdings import read_holdings
from pipeline_core.domain.type_refs import types_referenced_in_text
from pipeline_core.prompts.fix_prompts import FN_LITERAL_RULE
from pipeline_core.prompts.sections import render_types_section


@dataclass(frozen=True, slots=True)
class EvidenceSection:
    """A section the retry turn may carry, wrapped in `<tag>`; a section
    whose text already carries its own tags has `tag=""`."""

    tag: str
    text: str
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class AttemptFeedback:
    """What the harness measured about the reply it did not keep.

    `kind` is decided where the failure was measured (`LastErrorKind`), never
    re-read from the excerpt's prose. `targets` names what the next reply
    must carry bodies for, and `output` restates the route's parse contract
    (the `<BODY>` or `<FN name=… block=…>` form the opening turn asked for).
    `sections` are candidates; the renderer drops any the conversation
    already holds. `moved` is the text of bodies other fills rewrote on disk
    since the conversation's views of their files were rendered."""

    kind: LastErrorKind | None
    excerpt: str
    targets: tuple[str, ...]
    output: str
    sections: tuple[EvidenceSection, ...] = ()
    moved: str = ""
    note: str = ""


def _held(text: str, conversation: str) -> bool:
    body = text.strip()
    return bool(body) and body in conversation


def render_retry_turn(fb: AttemptFeedback, *, conversation: str) -> str:
    """The retry turn for `fb`, against everything the conversation has sent."""
    kind = resolve_kind(fb.kind, fb.excerpt)
    parts: list[str] = ["RESULT of your previous reply (it was not kept):"]
    if fb.excerpt.strip():
        tag = "LAST_ERROR" if kind.compiled else "COMPILE_ERRORS"
        parts.append(last_error_prefix(fb.excerpt, kind).rstrip())
        parts.append(f"<{tag}>\n{fb.excerpt.strip()}\n</{tag}>")
        directive = instruction_for(kind)
        if directive and directive not in fb.excerpt:
            parts.append(directive)
    if fb.note.strip():
        parts.append(fb.note.strip())
    for sec in fb.sections:
        if not sec.text.strip() or _held(sec.text, conversation):
            continue
        if not sec.tag:   # a section that carries its own tags
            parts.append(f"{sec.prefix}{sec.text.strip()}")
            continue
        parts.append(f"{sec.prefix}<{sec.tag}>\n{sec.text.strip()}\n</{sec.tag}>")
    if fb.moved.strip():
        parts.append(
            "Other fills rewrote these bodies on disk since the files above "
            "were shown; this is their current text:\n" + fb.moved.strip())
    named = ", ".join(f"`{t}`" for t in fb.targets) if fb.targets else "the same targets"
    parts.append(f"Reply with corrected bodies for {named}, in the same output "
                 f"format as before:\n{fb.output.strip()}")
    return "\n\n".join(parts)


def types_new_to(conversation: str, evidence: str,
                 records: Sequence[TypeRecord], *, viewer_file: str) -> str:
    """TYPES_IN_SCOPE entries for the crate types `evidence` (this result's
    excerpt) names, minus every type the conversation already defines. The
    rebuilt prompt used to re-select the whole section from the new error;
    on a transcript only the types the model has not been shown are new."""
    held = set(read_holdings(conversation).types)
    fresh = [r for r in types_referenced_in_text(
        evidence, list(records), visible_from=viewer_file)
        if r.name not in held]
    return render_types_section(fresh, viewer_file=viewer_file) if fresh else ""


#: The per-fn routes' output contract (`<BODY>`), restated on every turn.
BODY_OUTPUT = ("<BODY>\ncorrected body without surrounding braces\n</BODY>")

#: The per-fn fill route's contract when its reply may widen the write to
#: other functions of the file (`domain.joint_write`); restated on every turn
#: of that route only — the routes that cannot splice extras keep
#: `BODY_OUTPUT`.
PER_FN_JOINT_OUTPUT = BODY_OUTPUT + "\n" + JOINT_OUTPUT_CLAUSE


def fn_output(targets: Sequence[tuple[str, str]]) -> str:
    """The `<FN name=… block=…>` contract for (fn name, block header) pairs,
    the form the block and file routes parse (`extract_fn_bodies`)."""
    lines = [f'<FN name="{name}" block="{block or "free fns"}">\n'
             "  // body (no outer braces, no signature)\n</FN>"
             for name, block in targets]
    return "\n".join(lines or ['<FN name="..." block="...">\nbody\n</FN>']) \
        + "\n" + FN_LITERAL_RULE


def render_no_body_turn(output: str, targets: Sequence[str]) -> str:
    """The turn after a reply that carried no parsable body and no request:
    the reasoning is above, the answer did not arrive."""
    named = ", ".join(f"`{t}`" for t in targets) if targets else "the targets"
    return ("Your previous reply carried no body the harness could parse. "
            f"Reply now with the bodies for {named}, and nothing else, in "
            f"this format:\n{output.strip()}")
