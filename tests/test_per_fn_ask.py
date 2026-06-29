"""Tests for pipeline_core.adapters.per_fn_ask — ask_per_fn_body."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pipeline_core.adapters.per_fn_ask import ask_per_fn_body


@dataclass
class _FakeClient:
    """Minimal AskClient stand-in — returns responses from a script."""
    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _idx: int = field(default=0, init=False)

    def ask(self, prompt: str, system: str | None = None, model: str | None = None,
            max_tokens: int | None = None, think: bool | None = None,
            temperature: float | None = None) -> str:
        self.calls.append({"model": model, "think": think, "temperature": temperature})
        resp = self.responses[self._idx] if self._idx < len(self.responses) else ""
        self._idx += 1
        return resp


def _client(*responses: str) -> _FakeClient:
    return _FakeClient(responses=list(responses))


def test_first_attempt_good_body() -> None:
    c = _client("<BODY>a + b</BODY>")
    body, resp = ask_per_fn_body(c, "prompt", "add", [], sig_text="fn add", model="m")
    assert body == "a + b"
    assert len(c.calls) == 1
    assert c.calls[0].get("think") is None  # first attempt: think=default


def test_recovers_on_second_attempt() -> None:
    c = _client("<BODY>unimplemented!()</BODY>", "<BODY>a - b</BODY>")
    log: list[str] = []
    body, _ = ask_per_fn_body(c, "prompt", "sub", log, sig_text="fn sub", model="m")
    assert body == "a - b"
    assert any("recovered via think=False" in e for e in log)
    assert c.calls[1].get("think") is False


def test_all_stub_returns_none() -> None:
    c = _client(
        "<BODY>unimplemented!()</BODY>",
        "<BODY>unimplemented!()</BODY>",
        "<BODY>unimplemented!()</BODY>",
    )
    log: list[str] = []
    body, _ = ask_per_fn_body(c, "prompt", "f", log, sig_text="fn f", model="m")
    assert body is None
    assert len(log) == 3


def test_all_empty_returns_none() -> None:
    c = _client("", "", "")
    body, _ = ask_per_fn_body(c, "p", "f", [], sig_text="fn f", model="m")
    assert body is None


def test_unsafe_fn_lowers_temperature() -> None:
    c = _client("<BODY>*ptr</BODY>")
    ask_per_fn_body(c, "p", "f", [], sig_text="unsafe fn f()", model="m", unsafe_temp=0.05)
    assert c.calls[0].get("temperature") == pytest.approx(0.05)


def test_safe_fn_no_temperature() -> None:
    c = _client("<BODY>x + 1</BODY>")
    ask_per_fn_body(c, "p", "f", [], sig_text="fn f(x: i32) -> i32", model="m")
    assert c.calls[0].get("temperature") is None


def test_model_passed_through() -> None:
    c = _client("<BODY>todo!()</BODY>", "<BODY>1</BODY>")
    ask_per_fn_body(c, "p", "f", [], sig_text="fn f", model="my-model")
    assert c.calls[0]["model"] == "my-model"
    assert c.calls[1]["model"] == "my-model"


def test_last_response_returned() -> None:
    # all stubs → exhausts all 3 attempts; last_response = final stub
    c = _client("<BODY>unimplemented!()</BODY>",
                "<BODY>unimplemented!()</BODY>",
                "<BODY>unimplemented!()</BODY>")
    _, last = ask_per_fn_body(c, "p", "f", [], sig_text="fn f", model="m")
    assert last == "<BODY>unimplemented!()</BODY>"


def test_attempts_log_records_all_failures() -> None:
    c = _client("", "", "")
    log: list[str] = []
    ask_per_fn_body(c, "p", "fn_x", log, sig_text="fn fn_x", model="m")
    assert len(log) == 3
    assert all("fn_x" in e for e in log)
