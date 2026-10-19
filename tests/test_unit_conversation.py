"""The fill routes' one conversation engine (`unit_conversation`) and the
retry turn it sends (`prompts.retry_turn`).

Measured before it existed (2026-09-01 → 2026-09-24,
`scripts/prompt_sufficiency_census.py`): 690 mid-call requests, each answered
by re-sending the prompt with the answer glued on and the model's own request
turn dropped; every retry a rebuilt document. The session — the one route
that kept a transcript — paid 38.7 tests per 100 calls against per-fn 29.7,
file 12.6, pair 4.6, impl 0.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
)
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    TranscriptBound,
    UnitConversation,
)
from pipeline_core.domain.last_error import LastErrorKind
from pipeline_core.domain.prompt_holdings import FileView, file_block
from pipeline_core.prompts.retry_turn import (
    BODY_OUTPUT,
    AttemptFeedback,
    EvidenceSection,
    render_retry_turn,
)

_BODY = "<BODY>1</BODY>"


def _conv(replies: list[str], *, bound: int = 0, fit=lambda t: t,
          requests=None) -> tuple[UnitConversation, list[list[dict[str, Any]]]]:
    sent: list[list[dict[str, Any]]] = []
    seq = list(replies)

    def turns(messages, **_k):
        sent.append([dict(m) for m in messages])
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return UnitConversation(
        ConversationPorts(ask_turns=turns, log=lambda _m: None, fit=fit,
                          max_transcript_chars=bound),
        label="u", route="t", requests=requests), sent


def _fb(excerpt: str = "error[E0308]: mismatched types", **k: Any) -> AttemptFeedback:
    return AttemptFeedback(kind=k.pop("kind", LastErrorKind.COMPILE),
                           excerpt=excerpt, targets=("f",), output=BODY_OUTPUT, **k)


def test_a_retry_is_a_turn_after_the_models_reply() -> None:
    conv, sent = _conv(["<BODY>bad</BODY>", _BODY])
    conv.open("OPENING", has_deliverable=lambda r: "<BODY>" in r)
    conv.retry(_fb(), has_deliverable=lambda r: "<BODY>" in r)
    assert [m["role"] for m in sent[1]] == ["user", "assistant", "user"]
    assert sent[1][1]["content"] == "<BODY>bad</BODY>"
    assert "OPENING" not in sent[1][2]["content"]
    assert "E0308" in sent[1][2]["content"]


def test_only_the_opening_turn_is_fitted() -> None:
    fitted: list[str] = []

    def fit(t: str) -> str:
        fitted.append(t)
        return t[:5]

    conv, sent = _conv([_BODY, _BODY], fit=fit)
    conv.open("OPENING-LONG", has_deliverable=lambda _r: True)
    conv.retry(_fb(), has_deliverable=lambda _r: True)
    assert fitted == ["OPENING-LONG"]
    assert sent[1][0]["content"] == "OPENI"         # the prefix never changes


def test_a_turn_over_the_bound_is_not_sent() -> None:
    conv, sent = _conv(["x" * 50], bound=100)
    conv.open("p" * 40, has_deliverable=lambda _r: True)
    with pytest.raises(TranscriptBound):
        conv.retry(_fb("e" * 40), has_deliverable=lambda _r: True)
    assert len(sent) == 1 and len(conv.messages) == 2   # nothing half-appended


def test_control_with_no_bound_the_same_turn_is_sent() -> None:
    conv, sent = _conv(["x" * 50])
    conv.open("p" * 40, has_deliverable=lambda _r: True)
    conv.retry(_fb("e" * 40), has_deliverable=lambda _r: True)
    assert len(sent) == 2


def test_each_turn_restores_the_request_budget(tmp_path: Path) -> None:
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    req2 = '<REQUEST kind="type_def" arg="Own"/>'
    sources = PullSources(read_file=lambda _p: None, find_tests=lambda _n: "",
                          type_def=lambda n: f"struct {n};", full_error=lambda: "")
    conv, sent = _conv([req, _BODY, req2, _BODY], requests=make_request_answerer(
        sources, log=lambda _m: None, emit=lambda *_a, **_k: None, max_pulls=1))
    conv.open("P", has_deliverable=lambda r: "<BODY>" in r)
    conv.retry(_fb(), has_deliverable=lambda r: "<BODY>" in r)
    # Both turns' requests were answered (not refused): 4 calls, 2 answers.
    assert len(sent) == 4
    assert "struct Buf;" in str(sent[1][-1]["content"])
    assert "struct Own;" in str(sent[3][-1]["content"])


def test_a_section_the_conversation_holds_is_not_sent_again() -> None:
    held = "struct Buf { len: usize }"
    fb = _fb(sections=(EvidenceSection("TYPES_IN_SCOPE", held),
                       EvidenceSection("MODEL_NOTES", "a fresh note")))
    turn = render_retry_turn(fb, conversation=f"<TYPES_IN_SCOPE>\n{held}\n</TYPES_IN_SCOPE>")
    assert held not in turn and "a fresh note" in turn
    # Control: an empty conversation gets both.
    assert held in render_retry_turn(fb, conversation="")


def test_the_retry_names_the_failure_by_its_measured_kind() -> None:
    ran = render_retry_turn(_fb("thread 't' panicked at src/a.rs:3",
                                kind=LastErrorKind.REDDENED), conversation="")
    assert "<LAST_ERROR>" in ran and "compiled and then panicked" in ran
    rejected = render_retry_turn(_fb(), conversation="")
    assert "<COMPILE_ERRORS>" in rejected and "produced these errors" in rejected


_VIEW = """\
pub struct A;
impl A {
    pub fn f(&self) -> u8 { unimplemented!() }
    pub fn g(&self) -> u8 { unimplemented!() }
}
"""


def test_a_body_another_fill_rewrote_is_reported_once() -> None:
    conv, _ = _conv([_BODY])
    conv.open(file_block("src/a.rs", _VIEW, view=FileView.WHOLE),
              has_deliverable=lambda _r: True)
    disk = _VIEW.replace("fn g(&self) -> u8 { unimplemented!() }",
                         "fn g(&self) -> u8 { 7 }").replace(
        "fn f(&self) -> u8 { unimplemented!() }", "fn f(&self) -> u8 { 1 }")
    moved = conv.bodies_moved(lambda _p: disk, frozenset({"f"}))
    assert "pub fn g(&self) -> u8 { 7 }" in moved
    assert "fn f" not in moved                       # the unit's own target
    assert conv.bodies_moved(lambda _p: disk, frozenset({"f"})) == ""


def test_a_cfg_filtered_view_is_not_read_as_a_rewrite() -> None:
    raw = _VIEW.replace("    pub fn g(&self) -> u8 { unimplemented!() }\n",
                        "    pub fn g(&self) -> u8 {\n        #[cfg(off)]\n"
                        "        side();\n        0\n    }\n")
    view = raw.replace("        #[cfg(off)]\n        side();\n", "")
    conv, _ = _conv([_BODY])
    conv.open(file_block("src/a.rs", view, view=FileView.WHOLE),
              has_deliverable=lambda _r: True)

    def strip(t: str) -> str:
        return t.replace("        #[cfg(off)]\n        side();\n", "")

    assert conv.bodies_moved(lambda _p: raw, normalize=strip) == ""
    # Control: compared raw, the filtered view reads as a rewrite.
    assert "fn g" in conv.bodies_moved(lambda _p: raw)


# --- no fill path can call the model outside a conversation ----------------

_ROOT = Path(__file__).resolve().parents[1] / "pipeline_core"
_FILL_MODULES = (
    "application/reconstruct/impl_fill.py",
    "application/reconstruct/fill_one_fn.py",
    "application/reconstruct/fix_unit.py",
    "application/reconstruct/phase_c_engine.py",
    "application/reconstruct/slice_session.py",
    "adapters/per_fn_ask.py",
)


def _names_and_attrs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name)
    return out


def test_the_old_request_loop_is_gone_everywhere() -> None:
    for path in _ROOT.rglob("*.py"):
        assert "ask_with_pulls" not in _names_and_attrs(path), path


def test_no_fill_route_holds_a_single_prompt_model_call() -> None:
    """The routes reach the model only through `UnitConversation` (its
    `open`/`retry`/`reask`/`say`) — a module that calls `.ask(` or binds
    `deps.ask` could send a retry to a model without its reply."""
    for rel in _FILL_MODULES:
        names = _names_and_attrs(_ROOT / rel)
        assert "ask" not in names, rel
        assert "ask_raw" not in names, rel
