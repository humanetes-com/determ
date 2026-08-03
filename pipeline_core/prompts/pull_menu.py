"""Pull-protocol prompt fragments — the menu the model reads and the wrapper
around an answered request.

Guidance is given as concrete WRONG→RIGHT pairs because models act on examples
rather than on abstract rules. The names in those examples (`Buf`, `src/b.rs`)
are deliberate placeholders: an example naming a type the crate under
reconstruction actually declares would hand the model part of the answer it is
being measured on.
"""
from __future__ import annotations

from collections.abc import Sequence

from pipeline_core.domain.context_pull import ContextPull, PullAnswer, PullKind

_KIND_LINES = {
    PullKind.FILE: 'file — a path inside this crate, e.g. arg="src/helper.rs"',
    PullKind.TEST_SOURCE: 'test_source — a test fn name, e.g. arg="test_roundtrip"',
    PullKind.TYPE_DEF: 'type_def — a type name, e.g. arg="Wrapper"',
    PullKind.FULL_ERROR: "full_error — the complete last compiler output (no arg)",
    PullKind.MACRO_EXPANSION:
        'macro_expansion — expanded macros of a file, e.g. arg="src/lib.rs"',
}


def pull_menu_section(kinds: tuple[PullKind, ...], max_pulls: int) -> str:
    """The menu appended to a fill prompt. Empty when no kinds are servable."""
    if not kinds:
        return ""
    lines = "\n".join(f"  {_KIND_LINES[k]}" for k in kinds if k in _KIND_LINES)
    return f"""

INSTEAD OF GUESSING you may request context by replying with the tag(s) and
nothing else:
<REQUEST kind="KIND" arg="ARG"/>
Available kinds:
{lines}
WRONG (invents a field it cannot see):
    self.buf.push(c)
RIGHT (asks for the type first, then uses its REAL fields):
    <REQUEST kind="type_def" arg="Buf"/>
WRONG (guesses another file's helper):
    helper::render(x)
RIGHT (reads the file first, then implements against it):
    <REQUEST kind="file" arg="src/helper.rs"/>
You have {max_pulls} request(s) for this whole task, and each tag spends one.
ASK FOR EVERYTHING YOU NEED IN ONE REPLY — several tags in one reply are all
answered together, and asking one at a time is how the budget runs out before
you have written anything:
RIGHT (one reply, two tags, one round-trip):
    <REQUEST kind="type_def" arg="Buf"/><REQUEST kind="file" arg="src/b.rs"/>
When you have what you need — or have none left — return the FN block(s)."""


def render_pull_followups(answers: Sequence[PullAnswer]) -> str:
    """Appended to the prompt for the re-ask after request(s) are answered.

    One `<REQUESTED_CONTEXT>` block per answer and ONE closing instruction:
    repeating "now complete the task" after each block reads as several tasks."""
    if not answers:
        return ""
    blocks = []
    for answer in answers:
        body = (answer.payload if answer.ok
                else f"(not available: {answer.payload})")
        trunc = ' truncated="true"' if answer.truncated else ""
        blocks.append(f"\n\nYou requested: {answer.pull.tag()}\n"
                      f"<REQUESTED_CONTEXT{trunc}>\n{body}\n"
                      "</REQUESTED_CONTEXT>")
    return ("".join(blocks)
            + "\nNow complete the ORIGINAL task above and return the FN "
              "block(s).")


def render_pull_budget_spent(pending: Sequence[ContextPull]) -> str:
    """Appended for the ONE re-ask after the budget is spent and the model has
    asked anyway.

    Without this the request-only reply is returned to the caller, which sees a
    reply with no bodies and discards the whole attempt — observed 2026-08-03
    12:02 UTC on nightly src/error.rs, where a file-first fill that had already
    diagnosed the crash correctly ("`object_drop_rest` drops the entire
    `ErrorImpl<E>` including the already-extracted `_error` field") ended as
    `empty/no FN blocks (0 FN tag(s) in reply)`. The model is not refusing to
    answer; it is waiting for a reply that is never coming and has not been
    told so."""
    asked = " ".join(p.tag() for p in pending)
    return (f"\n\nYou requested: {asked}\n"
            "<REQUESTED_CONTEXT>\n(not available: you have no context "
            "requests left for this task — this is the last exchange)\n"
            "</REQUESTED_CONTEXT>\n"
            "Do NOT request anything further; another request tag ends this "
            "task with nothing written. Answer the ORIGINAL task NOW with the "
            "best FN block(s) you can write from what you already have, and "
            "state any assumption you had to make as a `//` comment in the "
            "body.")
