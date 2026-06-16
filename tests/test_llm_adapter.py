"""Unit tests for the thin LLM adapter (pipeline_core/adapters/llm.py).

ModelInteractLLM is a pass-through to model_interact.ask carrying the default
model/max_tokens; these tests fake mi.ask to assert the kwargs it forwards (and that
max_tokens is omitted when unset, so the controller's data-driven default applies).
"""
from __future__ import annotations

from typing import Any

import pytest

from pipeline_core.adapters import llm as llm_mod
from pipeline_core.adapters.llm import ModelInteractLLM


@pytest.fixture
def captured_ask(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace mi.ask with a recorder; return the dict that captures call kwargs."""
    seen: dict[str, Any] = {}

    def fake_ask(prompt: str, **kwargs: Any) -> str:
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        return "RESPONSE"

    monkeypatch.setattr(llm_mod.mi, "ask", fake_ask)
    return seen


def test_ask_forwards_model_system_think(captured_ask: dict[str, Any]) -> None:
    llm = ModelInteractLLM(default_model="m1")
    out = llm.ask("hello", system="sys", think=True)
    assert out == "RESPONSE"
    assert captured_ask["prompt"] == "hello"
    kw = captured_ask["kwargs"]
    assert kw["model"] == "m1" and kw["system"] == "sys" and kw["think"] is True
    assert "max_tokens" not in kw  # unset → omitted, controller default applies


def test_ask_includes_max_tokens_when_set(captured_ask: dict[str, Any]) -> None:
    llm = ModelInteractLLM(default_model="m2", default_max_tokens=4096)
    llm.ask("p")
    kw = captured_ask["kwargs"]
    assert kw["max_tokens"] == 4096 and kw["model"] == "m2"
    assert kw["system"] is None and kw["think"] is None  # defaults forwarded as-is


def test_ask_defaults_model_none(captured_ask: dict[str, Any]) -> None:
    ModelInteractLLM().ask("p")
    assert captured_ask["kwargs"]["model"] is None
