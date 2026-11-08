"""Integration: the budget plans from the server's exact prompt count,
because the server's 400 carries no count.

Real vLLM on localhost, the pipeline's `VLLMTransport`. No pipeline model
call is made: the premise requests either fail validation or ask for one
token.

The class, run `497151-a6fa205b` (2026-09-22 20:36 UTC): four fill calls
opened together just after a few prompts under 2k tokens had moved the
learned chars-per-token average from 3.51 to 3.61, so the next long
prompts were under-counted and rejected. vLLM's rejection says "your prompt
contains at least N input tokens", and N is ctx - max_tokens + 1, the
threshold the request crossed, not the prompt's size. The retry replanned
from N, shrank by its 2 % pad each time, and `src/ensure.rs::new` ended
after three 400s (31140, 32531, 33950) as `input too long`, with no answer.

Asserted: `count_input_tokens` equals the `prompt_tokens` the server
reports serving the same messages. Premise tests: the 400's N is the
threshold (for a 60-token prompt it is 23 when max_tokens = ctx - 22), and
`/tokenize` accepts chat messages with the template kwargs. If vLLM ever
starts reporting the real count, the premise fails loudly and the retry's
bound can be trusted again.

Gated on `INTEGRATION=1` and a server answering on localhost:8000;
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os

import httpx
import pytest

from pipeline_core.adapters.llm_transports import (
    VLLMTransport,
    _parse_input_token_overflow,
    reasoning_template_kwargs,
)
from pipeline_core.domain.model_profile import EffortLevel, resolve_model_knobs

_BASE = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
_ROOT = _BASE.rstrip("/").removesuffix("/v1")


def _served() -> tuple[str, int] | None:
    try:
        r = httpx.get(f"{_BASE.rstrip('/')}/models", timeout=5.0)
        m = r.json()["data"][0]
        return str(m["id"]), int(m["max_model_len"])
    except Exception:  # noqa: BLE001 — no server, no test
        return None


_SERVED = _served() if os.environ.get("INTEGRATION") == "1" else None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_SERVED is None,
                       reason="needs INTEGRATION=1 and vLLM on localhost"),
]

_MESSAGES = [{"role": "user", "content": "hello world hello world"}]


def _complete(model: str, max_tokens: int, *, thinking: bool = False,
              effort: EffortLevel = EffortLevel.MAX) -> httpx.Response:
    return httpx.post(
        f"{_BASE.rstrip('/')}/chat/completions", timeout=120.0,
        json={"model": model, "messages": _MESSAGES, "max_tokens": max_tokens,
              "chat_template_kwargs": reasoning_template_kwargs(
                  model, thinking, effort=effort)})


def _transport(monkeypatch: pytest.MonkeyPatch, model: str,
               ctx: int) -> VLLMTransport:
    monkeypatch.setenv("VLLM_BASE_URL", _BASE)
    monkeypatch.setenv("VLLM_MODEL_NAME", model)
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", str(ctx))
    return VLLMTransport()


def test_the_count_is_the_prompt_the_server_serves(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert _SERVED is not None
    model, ctx = _SERVED
    served = _complete(model, 1).json()["usage"]["prompt_tokens"]
    counted = _transport(monkeypatch, model, ctx).count_input_tokens(
        _MESSAGES, enable_thinking=False, effort=EffortLevel.MAX)
    assert counted == served


def test_premise_the_template_renders_each_levels_word(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The level's word is in the prompt the server serves, so the count
    must be taken at the call's level (`count_input_tokens(effort=)`): one
    count for both levels is the count of a different prompt. When the
    served profile sends one word for both levels there is nothing to tell
    apart."""
    assert _SERVED is not None
    model, ctx = _SERVED
    knobs = resolve_model_knobs(dict(os.environ), model)
    if knobs.effort_word(EffortLevel.MEDIUM) == knobs.effort_word(EffortLevel.MAX):
        pytest.skip("the served profile sends one word for both levels")
    tr = _transport(monkeypatch, model, ctx)
    counts = {lv: tr.count_input_tokens(_MESSAGES, enable_thinking=True, effort=lv)
              for lv in EffortLevel}
    served = {lv: _complete(model, 1, thinking=True, effort=lv).json()["usage"][
        "prompt_tokens"] for lv in EffortLevel}
    # Equal counts per level, not distinct ones: two words can tokenize to
    # the same length. Whether the template reads the word at all is the
    # launch conformance check's (`check_levels`, token ids).
    assert counts == served


def test_premise_the_rejection_reports_the_threshold_not_the_prompt() -> None:
    assert _SERVED is not None
    model, ctx = _SERVED
    served = _complete(model, 1).json()["usage"]["prompt_tokens"]
    max_tokens = ctx - 22
    r = _complete(model, max_tokens)
    assert r.status_code == 400
    bound = _parse_input_token_overflow(r.text)
    assert bound == ctx - max_tokens + 1
    assert bound < served


def test_negative_control_an_unreachable_server_falls_back_to_the_estimate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    assert _SERVED is not None
    model, ctx = _SERVED
    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("VLLM_MODEL_NAME", model)
    assert VLLMTransport().count_input_tokens(
        _MESSAGES, enable_thinking=False, effort=EffortLevel.MAX) is None
