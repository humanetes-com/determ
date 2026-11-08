"""A request answer is a repeat only when the conversation DELIVERED it.

Run `3323407-984b07f2` (2026-11-08), fill 3, slice session
`session[4 test(s), 16 unit(s)]`: a follow-up of 43,477 chars that carried
`src/fmt.rs` would have crossed the transcript bound, so the session replaced
it with a refusal ("request one smaller artifact"). The model then asked for
`src/fmt.rs` alone (3,009 chars, which fits) and was answered "unchanged since
it was sent above for this same request — not repeated". The resolver had
recorded the file as served when it RESOLVED it; the session dropped it. The
same replaced follow-up carried the sentence admitting `src/fmt.rs::debug` to
the write set, so the model was never told it could write that unit.

Claims (invented shape):
- after an over-bound refusal, a fitting request for one of the refused files
  carries the file;
- the admission sentence goes out with the refusal;
- a unit conversation whose request follow-up was never appended (a model
  error pops the turn) answers the same request in full on the next turn;
- the rule is the same for a non-file kind;
- a request answered and delivered is still not re-sent.
Negative controls: each claim's repeat decision taken on a conversation that
holds the delivered block flags the repeat, and the pre-fix side map (served
on resolve) flags it on the conversation that does not.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct import slice_session as ss
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
)
from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    UnitConversation,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.prompt_holdings import (
    ANSWER_REPEAT_BODY,
    answer_block,
    read_holdings,
)
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading
from pipeline_core.prompts.pull_menu import render_pull_followups

_NOT_REPEATED = "not repeated"
_BIG = "// big\n" * 6_000          # ~42 KB, over the session's room
_SMALL = "pub struct Report;\nimpl Report { pub fn display(&self) {} }\n"


def _sources(root: Path) -> PullSources:
    def read(rel: str) -> str | None:
        p = root / rel
        return p.read_text() if p.is_file() else None
    return PullSources(read_file=read, find_tests=lambda _n: "",
                       type_def=lambda n: f"pub struct {n} {{ pub x: u8 }}",
                       full_error=lambda: "")


def _answerer(root: Path, max_pulls: int = 10):
    return make_request_answerer(_sources(root), log=lambda _m: None,
                                 emit=lambda *_a, **_k: None, max_pulls=max_pulls)


# ── the grammar: one writer, one reader ─────────────────────────────────────

def test_an_answer_block_reads_back_as_delivered_and_a_stub_does_not() -> None:
    tag = '<REQUEST kind="file" arg="src/a.rs"/>'
    body = "line 1\nline 2\n"
    text = "prefix" + answer_block(tag, body) + "\nNow complete the task."
    assert read_holdings(text).delivered(tag) == body
    stub = answer_block(tag, ANSWER_REPEAT_BODY)
    assert read_holdings(stub).delivered(tag) is None
    refused = answer_block(tag, "(not available: no such file in this crate: src/a.rs)")
    assert read_holdings(refused).delivered(tag) is None
    # The renderer the resolver uses writes the same grammar.
    from pipeline_core.domain.context_pull import ContextPull, PullAnswer, PullKind
    pull = ContextPull(PullKind.FILE, "src/a.rs")
    rendered = render_pull_followups([(PullAnswer(pull, body), False)])
    assert read_holdings(rendered).delivered(pull.tag()) == body


# ── the session ─────────────────────────────────────────────────────────────

_U = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_U: FnRecord(file="src/lib.rs", fn_name="fmt",
                      container="impl fmt::Display for Report",
                      sig_text="fn fmt(&self) -> fmt::Result", body_text="BASE")}
_DISPLAY = FnRecord(file="src/fmt.rs", fn_name="display", container="impl Report",
                    sig_text="pub fn display(&self)", body_text="")


def _reading() -> TestStateReading:
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=frozenset({"t_keep"}),
        failed=frozenset({"t_fmt"}), pass_count=1, fail_count=1),))


def _session(root: Path, replies: list[str], *, bound: int,
             all_records: tuple[FnRecord, ...] = ()) -> list[str]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.rs").write_text("BASE")
    (root / "src" / "big.rs").write_text(_BIG)
    (root / "src" / "fmt.rs").write_text(_SMALL)
    seen: list[str] = []
    queue = list(replies)

    def ask(messages: list[dict]) -> str:
        seen.append(str(messages[-1].get("content") or ""))
        return queue.pop(0) if queue else "no change"

    ports = SessionPorts(
        ask_turns=ask,
        splice=lambda *a, **k: (True, "", 0),
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw, _role=None: _reading(),
        read_source=lambda p: None,
        enumerate_bodies=lambda: {_U: "BASE"},
        answer_pulls=_answerer(root),
        log=lambda m: None,
        emit=lambda ev, data=None, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt",), units=(_U,)),
        records=dict(_RECS), turn0="turn0", max_turns=4,
        max_transcript_chars=bound, all_records=all_records,
        request_allowance=4)
    return seen


_BOTH = ('<REQUEST kind="file" arg="src/big.rs"/>'
         '<REQUEST kind="file" arg="src/fmt.rs"/>')
_FMT = '<REQUEST kind="file" arg="src/fmt.rs"/>'


def test_after_an_over_bound_refusal_the_smaller_request_carries_the_file(
        tmp_path: Path) -> None:
    seen = _session(tmp_path, [_BOTH, _FMT], bound=30_000 + ss._RESULT_ROOM)
    assert "was not sent" in seen[1] and "// big" not in seen[1]
    assert "pub struct Report;" in seen[2]
    assert _NOT_REPEATED not in seen[2]


def test_an_answer_that_was_delivered_is_still_not_re_sent(tmp_path: Path) -> None:
    seen = _session(tmp_path, [_FMT, _FMT], bound=0)
    assert "pub struct Report;" in seen[1]
    assert _NOT_REPEATED in seen[2] and "pub struct Report;" not in seen[2]


def test_the_admission_sentence_rides_with_the_refusal(tmp_path: Path) -> None:
    seen = _session(tmp_path, [_BOTH, "no change"],
                    bound=30_000 + ss._RESULT_ROOM, all_records=(_DISPLAY,))
    assert "was not sent" in seen[1]
    assert "now in the session's editable set" in seen[1]
    assert "display (impl Report) in src/fmt.rs" in seen[1]


def test_control_the_admission_is_lost_when_the_refusal_replaces_it() -> None:
    """The pre-fix order: the sentence appended, then the whole follow-up
    replaced."""
    admission = ss.render_admitted_units((_DISPLAY,))
    followup = "x" * 50_000 + admission
    replaced = ss.render_request_over_bound(len(followup), 1_000)
    assert "editable set" not in replaced


# ── the unit conversation ───────────────────────────────────────────────────

def test_a_follow_up_the_model_error_dropped_is_answered_again(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fmt.rs").write_text(_SMALL)
    sent: list[list[dict[str, Any]]] = []
    calls = {"n": 0}

    def turns(messages, **_k):
        calls["n"] += 1
        sent.append([dict(m) for m in messages])
        if calls["n"] == 2:
            raise RuntimeError("model error on the follow-up")
        return _FMT if calls["n"] == 1 else "<BODY>1</BODY>"

    conv = UnitConversation(
        ConversationPorts(ask_turns=turns, log=lambda _m: None, fit=lambda t: t,
                          max_transcript_chars=0),
        label="u", route="t", requests=_answerer(tmp_path))
    with contextlib.suppress(RuntimeError):
        conv.open("P", has_deliverable=lambda r: "<BODY>" in r)
    assert "pub struct Report;" not in conv.text()      # never delivered
    reply = conv._answered(_FMT, lambda r: "<BODY>" in r)
    assert reply == "<BODY>1</BODY>"
    assert "pub struct Report;" in str(sent[-1][-1]["content"])


# ── a non-file kind ─────────────────────────────────────────────────────────

def test_a_type_def_refused_once_is_answered_in_full_next(tmp_path: Path) -> None:
    answer = _answerer(tmp_path)
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    first = answer(req, conversation="turn0") or ""
    assert "pub struct Buf" in first
    # The caller dropped `first`; the conversation never carried it.
    again = answer(req, conversation="turn0\n\nRESULT: not sent") or ""
    assert "pub struct Buf" in again and _NOT_REPEATED not in again
    # Control: on a conversation that did carry it, the same call flags it.
    third = answer(req, conversation="turn0" + first) or ""
    assert _NOT_REPEATED in third and "pub struct Buf" not in third


def test_control_a_map_filled_on_resolve_claims_the_dropped_answer() -> None:
    """The pre-fix rule: `served[pull] = payload` at resolve time, and a repeat
    is `served.get(pull) == payload`, whatever the caller delivered."""
    from pipeline_core.domain.context_pull import ContextPull, PullKind
    pull = ContextPull(PullKind.TYPE_DEF, "Buf")
    payload = "pub struct Buf { pub x: u8 }"
    served: dict[ContextPull, str] = {pull: payload}     # resolved, then dropped
    assert served.get(pull) == payload                   # claimed as sent
    assert read_holdings("turn0\n\nRESULT: not sent").delivered(pull.tag()) is None
