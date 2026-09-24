"""Tests for pipeline_core.adapters.per_fn_ask — the per-fn extraction
ladder as turns on the unit's conversation, and its factory."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pipeline_core.adapters.per_fn_ask import ask_per_fn_body, conversation_factory
from pipeline_core.domain.last_error import LastErrorKind
from pipeline_core.prompts.retry_turn import BODY_OUTPUT, AttemptFeedback


@dataclass
class _FakeClient:
    """Minimal AskClient stand-in: a transcript transport over a script."""
    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _idx: int = field(default=0, init=False)

    def ask_turns(self, messages: list[dict[str, Any]], *, model: str | None = None,
                  max_tokens: int | None = None, think: bool | None = None,
                  temperature: float | None = None,
                  followup_think: bool = False) -> str:
        self.calls.append({"model": model, "think": think, "temperature": temperature,
                           "messages": [dict(m) for m in messages]})
        resp = self.responses[self._idx] if self._idx < len(self.responses) else ""
        self._idx += 1
        return resp

    def fit(self, prompt: str) -> str:
        return prompt


def _open(c: _FakeClient, model: str = "m") -> Any:
    return conversation_factory(c, model=model, log=lambda _s: None,
                                emit=lambda *_a, **_k: None,
                                max_transcript_chars=0)("unit")


def _client(*responses: str) -> _FakeClient:
    return _FakeClient(responses=list(responses))


def _ask(c: _FakeClient, turn: Any, fn: str, log: list[str], **k: Any) -> Any:
    conv = k.pop("conversation", None) or _open(c, k.pop("model", "m"))
    return ask_per_fn_body(turn, fn, log, conversation=conv, **k)


def test_first_attempt_good_body() -> None:
    c = _client("<BODY>a + b</BODY>")
    body, resp = _ask(c, "prompt", "add", [], sig_text="fn add")
    assert body == "a + b"
    assert len(c.calls) == 1
    assert c.calls[0].get("think") is None  # first attempt: think=default


def test_recovers_on_second_attempt() -> None:
    c = _client("<BODY>unimplemented!()</BODY>", "<BODY>a - b</BODY>")
    log: list[str] = []
    body, _ = _ask(c, "prompt", "sub", log, sig_text="fn sub")
    assert body == "a - b"
    assert any("recovered via think=False" in e for e in log)
    assert c.calls[1].get("think") is False


def test_a_rung_is_a_turn_on_the_same_transcript() -> None:
    """The re-ask carries the prompt and the model's stub reply above it,
    then asks for the body — never the prompt again as a fresh document."""
    c = _client("<BODY>unimplemented!()</BODY>", "<BODY>a - b</BODY>")
    _ask(c, "THE PROMPT", "sub", [], sig_text="fn sub")
    second = c.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[0]["content"] == "THE PROMPT"
    assert second[1]["content"] == "<BODY>unimplemented!()</BODY>"
    assert "THE PROMPT" not in second[2]["content"]
    assert BODY_OUTPUT.strip() in second[2]["content"]


def test_a_retry_turn_follows_the_opening_on_one_conversation() -> None:
    c = _client("<BODY>wrong()</BODY>", "<BODY>right()</BODY>")
    conv = _open(c)
    _ask(c, "OPENING", "f", [], sig_text="fn f", conversation=conv)
    body, _ = _ask(c, AttemptFeedback(
        kind=LastErrorKind.COMPILE, excerpt="error[E0425]: cannot find `x`",
        targets=("f",), output=BODY_OUTPUT), "f", [], sig_text="fn f",
        conversation=conv)
    assert body == "right()"
    sent = c.calls[1]["messages"]
    assert sent[0]["content"] == "OPENING"
    assert sent[1]["content"] == "<BODY>wrong()</BODY>"
    assert "E0425" in sent[2]["content"]
    assert "OPENING" not in sent[2]["content"]


def test_all_stub_returns_none() -> None:
    c = _client(
        "<BODY>unimplemented!()</BODY>",
        "<BODY>unimplemented!()</BODY>",
        "<BODY>unimplemented!()</BODY>",
    )
    log: list[str] = []
    body, _ = _ask(c, "prompt", "f", log, sig_text="fn f")
    assert body is None
    assert len(log) == 3


def test_all_empty_returns_none() -> None:
    c = _client("", "", "")
    body, _ = _ask(c, "p", "f", [], sig_text="fn f")
    assert body is None


def test_unsafe_fn_lowers_temperature() -> None:
    c = _client("<BODY>*ptr</BODY>")
    _ask(c, "p", "f", [], sig_text="unsafe fn f()", unsafe_temp=0.05)
    assert c.calls[0].get("temperature") == pytest.approx(0.05)


def test_safe_fn_no_temperature() -> None:
    c = _client("<BODY>x + 1</BODY>")
    _ask(c, "p", "f", [], sig_text="fn f(x: i32) -> i32")
    assert c.calls[0].get("temperature") is None


def test_model_passed_through() -> None:
    c = _client("<BODY>todo!()</BODY>", "<BODY>1</BODY>")
    _ask(c, "p", "f", [], sig_text="fn f", model="my-model")
    assert c.calls[0]["model"] == "my-model"
    assert c.calls[1]["model"] == "my-model"


def test_last_response_returned() -> None:
    # all stubs → exhausts all 3 attempts; last_response = final stub
    c = _client("<BODY>unimplemented!()</BODY>",
                "<BODY>unimplemented!()</BODY>",
                "<BODY>unimplemented!()</BODY>")
    _, last = _ask(c, "p", "f", [], sig_text="fn f")
    assert last == "<BODY>unimplemented!()</BODY>"


def test_attempts_log_records_all_failures() -> None:
    c = _client("", "", "")
    log: list[str] = []
    _ask(c, "p", "fn_x", log, sig_text="fn fn_x")
    assert len(log) == 3
    assert all("fn_x" in e for e in log)
