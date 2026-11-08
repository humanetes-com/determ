"""Per-call reasoning budget: each thinking call gets the room its own prompt
leaves in the context, with no fixed character cap.

The defect it closes, measured 2026-09-21 on runs `450977-5f48b68a`,
`195091-72b92f1e` and `3400-4d0618ba`: under the fixed shape
(`VLLM_THINKING_BUDGET=16384`, `VLLM_MAX_THINK_CHARS=120000`) the file/impl/pair
route's healthy reasoning ran p50 73,962 / max 119,973 chars and 14% of its
calls were cut at 120,000. Re-asked with no cap
(`scripts/measure_capped_uncapped.py`), the cut prompts finished on their own;
the first seven are the recorded fixture below. Raising the fixed cap instead
shrinks every prompt, because `settings.safe_input_char_limit` reserves the
thinking budget out of prompt space.
"""

from __future__ import annotations

import pytest

from pipeline_core.domain.model_caps import safe_input_char_limit
from pipeline_core.domain.model_profile import EffortLevel, reasoning_budget_is_per_call
from pipeline_core.llm import BudgetSpec, BudgetState, Delta, Hooks, InputTooLong, LLMController
from pipeline_core.llm.budget import plan_for_input, request_max_tokens
from tests.test_llm_controller import FakeTransport

CTX = 131_072
CONTENT = 16_384
FLOOR = 32_768
SENTINEL = 32_768
FIXED_CAP = 120_000
# Reasoning chars per token measured on the recorded reasoning streams
# (the `.env` sizing note, 2026-08-27).
REASONING_CPT = 4.25

# (prompt chars, uncapped reasoning chars) — the cut prompts re-asked with no
# cap on 2026-09-21; every one finished with finish=stop.
MEASURED = [
    (22_366, 155_176), (31_695, 96_368), (43_291, 93_740), (26_512, 127_950),
    (103_392, 101_209), (27_633, 140_765), (27_633, 159_457),
]


def _spec(*, per_call: bool, **kw):
    base = {
        "ctx_len": CTX, "content_budget": CONTENT, "thinking_budget": FLOOR,
        "margin": 512, "cpt_floor": 3.0, "adaptive_cpt": True,
        "add_thinking_to_request": True, "cap_by_ctx": True,
        "send_max_tokens": True, "thinking_char_cap": FIXED_CAP,
        "per_call_reasoning": per_call,
    }
    base.update(kw)
    return BudgetSpec(**base)


def _plan(spec, prompt_chars, *, thinking=True, cpt=3.66):
    return request_max_tokens(spec, BudgetState(cpt=cpt), input_chars=prompt_chars,
                              caller_max_tokens=SENTINEL, default_sentinel=SENTINEL,
                              enable_thinking=thinking)


@pytest.mark.parametrize(("prompt_chars", "reasoning_chars"), MEASURED)
def test_every_measured_uncapped_call_fits_its_per_call_room(prompt_chars, reasoning_chars):
    plan = _plan(_spec(per_call=True), prompt_chars)
    assert plan.thinking_char_cap is None
    assert plan.request_max is not None
    assert plan.est_input_tokens + plan.request_max <= CTX - 512
    assert plan.content_cap == CONTENT
    assert plan.thinking_reserve * REASONING_CPT > reasoning_chars


def test_negative_control_fixed_shape_cuts_the_measured_calls():
    plan = _plan(_spec(per_call=False, thinking_budget=16_384), 22_366)
    assert plan.thinking_char_cap == FIXED_CAP
    cut = [r for _, r in MEASURED if r >= plan.thinking_char_cap]
    assert len(cut) == 4


def test_largest_planned_prompt_still_gets_the_floor():
    largest = safe_input_char_limit(CTX, FLOOR, CONTENT)
    plan = _plan(_spec(per_call=True), largest, cpt=3.0)
    assert plan.request_max is not None
    assert plan.est_input_tokens + plan.request_max <= CTX - 512
    assert plan.thinking_reserve >= FLOOR


def test_small_prompt_gets_more_than_the_floor():
    plan = _plan(_spec(per_call=True), 22_366)
    assert plan.thinking_reserve > 3 * FLOOR


def test_thinking_off_is_content_only_in_both_shapes():
    for per_call in (True, False):
        plan = _plan(_spec(per_call=per_call), 22_366, thinking=False)
        assert plan.thinking_reserve == 0
        assert plan.request_max == CONTENT


def test_retry_replans_per_call_from_the_padded_server_count():
    spec = _spec(per_call=True)
    first = plan_for_input(spec, input_tokens=10_000, content_cap=CONTENT,
                           enable_thinking=True)
    real = 60_000
    t = FakeTransport([InputTooLong(real), [Delta(content="ok")]], spec=spec)
    out = LLMController(Hooks(), default_sentinel=SENTINEL).run(
        t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "ok"
    retry = t.calls[1]["max_tokens"]
    padded = int(real * 1.02) + 256
    assert retry == CTX - padded - 512
    assert first.thinking_char_cap is None


def test_controller_streams_per_call_without_the_fixed_cap():
    reasoning = "x" * (FIXED_CAP + 5_000)
    spec = _spec(per_call=True)
    t = FakeTransport([[Delta(reasoning=reasoning), Delta(content="body")]], spec=spec)
    out = LLMController(Hooks(), default_sentinel=SENTINEL).run(
        t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "body"
    assert len(t.calls) == 1


def test_controller_negative_control_fixed_shape_breaks_and_follows_up():
    reasoning = "x" * (FIXED_CAP + 5_000)
    spec = _spec(per_call=False)
    t = FakeTransport([[Delta(reasoning=reasoning), Delta(content="body")],
                       [Delta(content="followup body")]], spec=spec)
    out = LLMController(Hooks(), default_sentinel=SENTINEL).run(
        t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "followup body"
    assert len(t.calls) == 2


@pytest.mark.parametrize(("value", "expected"), [
    ("per_call", True), ("per-call", True), ("PER_CALL", True),
    ("fixed", False), ("", False),
])
def test_reasoning_budget_knob_parses(value, expected):
    assert reasoning_budget_is_per_call(value) is expected


def test_reasoning_budget_knob_refuses_a_typo():
    with pytest.raises(ValueError, match="VLLM_REASONING_BUDGET"):
        reasoning_budget_is_per_call("percall")


# ── the model profile's runaway cap under per_call ──────────────────────────
# The per_call branch returns the profile cap: None for Muse (its uncapped tail
# finished naturally, MEASURED above), a measured number for a calibrated
# model (Qwen3.8, 2026-11-06), and none while a profile is uncalibrated.

RUNAWAY = 90_000


@pytest.mark.parametrize(("cap", "expected"), [
    (None, None),          # Muse: calibrated, no cap
    (RUNAWAY, RUNAWAY),    # a measured cap reaches the plan
])
def test_per_call_plan_carries_the_profile_cap(cap, expected):
    plan = _plan(_spec(per_call=True, per_call_char_cap=cap), 22_366)
    assert plan.thinking_char_cap == expected
    # The fixed cap never leaks into a per-call plan.
    assert plan.thinking_char_cap != FIXED_CAP


def test_the_profile_runaway_cap_reaches_the_transport_spec(monkeypatch):
    import dataclasses

    import pipeline_core.domain.model_profile as mp
    from pipeline_core.adapters.llm_transports import VLLMTransport
    for v in ("VLLM_RUNAWAY_THINK_CHARS", "VLLM_REASONING_BUDGET", "MODEL_FAMILY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("VLLM_MODEL_NAME", "Qwen/Qwen3.8-27B-FP8")
    # Calibrated: the measured cap is the spec's.
    spec = VLLMTransport().spec(effort=EffortLevel.MAX)
    assert spec.per_call_reasoning is True and spec.per_call_char_cap == 335_000
    # Uncalibrated: no cap, never an invented one.
    monkeypatch.setattr(mp, "PROFILES", tuple(
        dataclasses.replace(p, runaway_think_chars=None, runaway_calibrated=False)
        if p.family == "qwen3" else p for p in mp.PROFILES))
    assert VLLMTransport().spec(effort=EffortLevel.MAX).per_call_char_cap is None
    # An env value overrides either.
    monkeypatch.setenv("VLLM_RUNAWAY_THINK_CHARS", str(RUNAWAY))
    assert VLLMTransport().spec(effort=EffortLevel.MAX).per_call_char_cap == RUNAWAY


def test_controller_breaks_a_per_call_stream_at_the_plan_cap():
    # The consume path breaks at the cap the PLAN carried and follows up.
    reasoning = "x" * (RUNAWAY + 5_000)
    spec = _spec(per_call=True, per_call_char_cap=RUNAWAY)
    t = FakeTransport([[Delta(reasoning=reasoning), Delta(content="body")],
                       [Delta(content="followup body")]], spec=spec)
    out = LLMController(Hooks(), default_sentinel=SENTINEL).run(
        t, system=None, prompt="p", model="m", max_tokens=SENTINEL)
    assert out == "followup body"
    assert len(t.calls) == 2
