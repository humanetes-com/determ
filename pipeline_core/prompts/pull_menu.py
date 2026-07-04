"""Pull-protocol prompt fragments (july_plan P2) — the menu the model reads
and the follow-up wrapper around an answered request. Concrete WRONG→RIGHT
examples per CLAUDE.md's LLM-guidance rule (models act on examples, not
abstract rules); generic placeholder names only (FR-036).
"""
from __future__ import annotations

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

INSTEAD OF GUESSING you may request ONE piece of context by replying with
ONLY this tag (nothing else in the reply):
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
You have at most {max_pulls} request(s) for this task; when you have what you
need — or have none left — return the FN block(s)."""


def render_pull_followup(pull: ContextPull, answer: PullAnswer) -> str:
    """Appended to the prompt for the re-ask after a request is answered."""
    body = (answer.payload if answer.ok
            else f"(not available: {answer.payload})")
    trunc = ' truncated="true"' if answer.truncated else ""
    return (f"\n\nYou requested: {pull.tag()}\n"
            f"<REQUESTED_CONTEXT{trunc}>\n{body}\n</REQUESTED_CONTEXT>\n"
            "Now complete the ORIGINAL task above and return the FN block(s).")
