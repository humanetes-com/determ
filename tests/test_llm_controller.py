"""Behavioural tests for the unified LLM control plane (pipeline_core.llm).

A FakeTransport scripts the token stream so the controller's recovery/cap/early-stop
policy is exercised with no real server. This is the net the per-backend duplication
removal must preserve.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pipeline_core.llm import (
    BudgetSpec,
    BudgetState,
    Delta,
    Hooks,
    InputTooLong,
    LLMController,
    StreamMeta,
)
from pipeline_core.llm.budget import request_max_tokens
from pipeline_core.llm.detect import StopDetector
from pipeline_core.llm.thinktag import InlineThinkSplitter

SENTINEL = 32768


def _spec(**kw):
    base = {
        "ctx_len": 65536, "content_budget": 16384, "thinking_budget": 16384, "margin": 512,
        "cpt_floor": 3.0, "adaptive_cpt": True, "add_thinking_to_request": True,
        "cap_by_ctx": True, "send_max_tokens": True, "thinking_char_cap": None,
    }
    base.update(kw)
    return BudgetSpec(**base)


class FakeTransport:
    """Scripts per-stream() responses; each is a list[Delta] or an Exception to raise."""

    served_model = "fake"
    supports_followup = True

    def __init__(self, responses, *, spec=None, finish_reasons=None, prompt_tokens=None,
                 thinking=True):
        self._responses = list(responses)
        self._finish = list(finish_reasons or [None] * len(responses))
        self._ptoks = list(prompt_tokens or [None] * len(responses))
        self._spec = spec or _spec()
        self._state = BudgetState()
        self._thinking = thinking
        self.calls: list[dict] = []

    def spec(self):
        return self._spec

    def budget_state(self):
        return self._state

    def resolve_thinking(self, model, want_think):
        return self._thinking if want_think is None else want_think

    def stream(self, messages, *, model, max_tokens, enable_thinking, meta: StreamMeta,
               temperature: float | None = None) -> Iterator[Delta]:
        idx = len(self.calls)
        self.calls.append({"messages": messages, "max_tokens": max_tokens,
                           "enable_thinking": enable_thinking, "temperature": temperature})
        resp = self._responses[idx]
        if isinstance(resp, Exception):
            raise resp
        meta.finish_reason = self._finish[idx]
        meta.prompt_tokens = self._ptoks[idx]
        yield from resp

    def followup_messages(self, base_messages, reasoning_text, *, model):
        return base_messages + [
            {"role": "assistant", "content": f"<think>{reasoning_text}</think>"},
            {"role": "user", "content": "code only"},
        ]

    def clean(self, text, *, model):
        return text.strip()

    def wait_until_ready(self):
        return None


def _controller():
    return LLMController(Hooks(), default_sentinel=SENTINEL)


# --------------------------------------------------------------------------- #
# StopDetector
# --------------------------------------------------------------------------- #
def test_stopdetector_block_close():
    d = StopDetector()
    assert not d.feed("mod gen_tests_1 {")
    assert not d.feed("fn a() { let x = 1; }")
    assert d.feed("}")  # outer brace closes
    assert d.reason == "block"


def test_stopdetector_file_close():
    d = StopDetector()
    assert not d.feed("<FILE x>body")
    assert d.feed("</FILE>")
    assert d.reason == "file"


# --------------------------------------------------------------------------- #
# InlineThinkSplitter
# --------------------------------------------------------------------------- #
def test_thinksplitter_basic():
    s = InlineThinkSplitter()
    c, r = s.feed("pre<think>reason</think>post")
    assert c == "prepost"
    assert r == "reason"


def test_thinksplitter_across_chunks():
    s = InlineThinkSplitter()
    out_c, out_r = "", ""
    for piece in ["a<thi", "nk>re", "as</thin", "k>b"]:
        c, r = s.feed(piece)
        out_c += c
        out_r += r
    fc, fr = s.finish()
    out_c += fc
    out_r += fr
    assert out_c == "ab"
    assert out_r == "reas"


def test_thinksplitter_unclosed_think_is_reasoning():
    s = InlineThinkSplitter()
    c, r = s.feed("<think>still going")
    fc, fr = s.finish()
    assert (c + fc) == ""
    assert (r + fr) == "still going"


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #
def test_budget_adds_thinking_reserve_and_caps_by_ctx():
    spec = _spec()
    plan = request_max_tokens(spec, BudgetState(cpt=4.0), input_chars=4000,
                              caller_max_tokens=SENTINEL, default_sentinel=SENTINEL,
                              enable_thinking=True)
    # content floored at 16384 (well under ctx headroom), + 16384 thinking reserve.
    assert plan.thinking_reserve == 16384
    assert plan.request_max == 16384 + 16384


def test_budget_explicit_caller_bound_is_honored():
    spec = _spec()
    plan = request_max_tokens(spec, BudgetState(cpt=4.0), input_chars=100,
                              caller_max_tokens=6144, default_sentinel=SENTINEL,
                              enable_thinking=True)
    # explicit 6144 content bound kept; thinking still gets its own reserve.
    assert plan.content_cap == 6144
    assert plan.request_max == 6144 + 16384


def test_budget_native_does_not_send_max_tokens():
    spec = _spec(send_max_tokens=False, cap_by_ctx=True)
    plan = request_max_tokens(spec, BudgetState(), input_chars=100,
                              caller_max_tokens=SENTINEL, default_sentinel=SENTINEL,
                              enable_thinking=True)
    assert plan.request_max is None


# --------------------------------------------------------------------------- #
# LLMController
# --------------------------------------------------------------------------- #
def test_controller_plain_content_no_followup():
    t = FakeTransport([[Delta(reasoning="thinking"), Delta(content="hello")]])
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "hello"
    assert len(t.calls) == 1  # no followup


def test_controller_thinking_only_triggers_followup():
    t = FakeTransport([
        [Delta(reasoning="deep reasoning"), Delta(content="")],  # no usable content
        [Delta(content="final code")],                           # followup
    ])
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "final code"
    assert len(t.calls) == 2
    assert t.calls[1]["enable_thinking"] is False  # followup disables thinking


def test_controller_length_truncation_triggers_followup():
    t = FakeTransport(
        [[Delta(reasoning="r"), Delta(content="partial")], [Delta(content="recovered")]],
        finish_reasons=["length", None],
    )
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "recovered"
    assert len(t.calls) == 2


def test_controller_think_cap_then_followup():
    spec = _spec(thinking_char_cap=10)
    t = FakeTransport(
        [[Delta(reasoning="x" * 50)], [Delta(content="code after cap")]],
        spec=spec,
    )
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "code after cap"
    assert len(t.calls) == 2


def test_controller_early_stop_on_block_close():
    # Detector stops the stream; anything after the closing brace is never consumed.
    t = FakeTransport([[
        Delta(content="mod gen_x {"),
        Delta(content=" fn a(){} "),
        Delta(content="}"),
        Delta(content="TRAILING ECHO SHOULD NOT APPEAR"),
    ]])
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert "TRAILING ECHO" not in out
    assert out.startswith("mod gen_x")


def test_controller_input_too_long_retries():
    t = FakeTransport([InputTooLong(5000), [Delta(content="ok after retry")]])
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "ok after retry"
    assert len(t.calls) == 2


def test_controller_input_retry_pads_lower_bound():
    # the server's real_tokens is a LOWER bound ("at least N") —
    # the replanned request must pad it (N*1.02 + 256) on top of the margin,
    # or the retry lands ~1 token over ctx forever (anyhow 2026-07-02).
    t = FakeTransport([InputTooLong(40172), [Delta(content="ok")]])
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "ok"
    retry_max = t.calls[1]["max_tokens"]
    assert retry_max is not None
    assert 40172 + retry_max <= 65536 - 512 - int(40172 * 0.02) - 256


def test_controller_no_followup_when_unsupported():
    t = FakeTransport([[Delta(reasoning="r"), Delta(content="")]])
    t.supports_followup = False
    out = _controller().run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == ""
    assert len(t.calls) == 1


def test_controller_persists_thinking_via_hook():
    persisted = {}
    hooks = Hooks(persist_thinking=lambda result, thinking: persisted.update(r=result, t=thinking))
    ctrl = LLMController(hooks, default_sentinel=SENTINEL)
    t = FakeTransport([[Delta(reasoning="why"), Delta(content="what")]])
    ctrl.run(t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert persisted == {"r": "what", "t": "why"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
