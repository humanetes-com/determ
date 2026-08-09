"""`llm.stream_meta` — the token/finish-reason provenance `llm.response` lacks.

`llm.response` (adapters/ask_client.py) records only
`{prompt_sha, response: {sha256, char_count}}`. `StreamMeta` already carries
`finish_reason` and `prompt_tokens`, and the controller already reads real usage
to drive `adaptive_cpt` — none of it reached the event log, so diagnosing run
`491052-1af6b4f4` needed `logs/pipeline_guards.log` (no run ids, no dates, spans
months) plus the vLLM Prometheus endpoint. Four calls that each burned ~20
minutes of GPU and returned zero characters were indistinguishable in
`events.jsonl` from ordinary slow calls.

The join is the load-bearing part: the sibling event must key on the same
`prompt_sha` the adapter emits, or it is most of the way back to the problem.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.text import PromptDigest
from pipeline_core.llm import Delta, Hooks, LLMController
from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

PROMPT = "fill src/error.rs::construct_from_boxed"


def _run(transport: FakeTransport, prompt: str = PROMPT) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ctl = LLMController(
        Hooks(emit=lambda name, data: events.append({"event": name, **data})),
        default_sentinel=SENTINEL)
    ctl.run(transport, system="sys", prompt=prompt, model="m", max_tokens=SENTINEL)
    return [e for e in events if e["event"] == "llm.stream_meta"]


def test_one_stream_meta_per_call() -> None:
    metas = _run(FakeTransport([[Delta(content="fn f() {}")]]))
    assert len(metas) == 1


def test_it_joins_to_llm_response_on_prompt_sha() -> None:
    """`ask_client` hashes the USER prompt alone via `store_prompt_content`,
    which is `PromptDigest.of`. The controller holds system+user as `messages`
    and must hash the same thing, or the two events cannot be joined and the
    provenance is unattributable."""
    metas = _run(FakeTransport([[Delta(content="x")]]))

    assert metas[0]["prompt_sha"] == PromptDigest.of(PROMPT).sha256
    assert metas[0]["prompt_sha"] != PromptDigest.of("sys" + PROMPT).sha256


def test_the_zero_content_runaway_is_distinguishable_from_a_short_reply() -> None:
    """The whole point. Both calls return no usable content; only one burned the
    budget. `char_count: 0` on `llm.response` cannot tell them apart."""
    runaway = FakeTransport(
        [[Delta(reasoning="r" * 121_481)], []],
        spec=_spec(thinking_char_cap=None), finish_reasons=["length", None])
    empty = FakeTransport([[Delta(content="")], []], finish_reasons=[None, None])

    burned, quiet = _run(runaway)[0], _run(empty)[0]

    assert burned["content_chars"] == quiet["content_chars"] == 0
    assert burned["finish_reason"] == "length"
    assert burned["reasoning_chars"] == 121_481
    assert quiet["reasoning_chars"] == 0
    assert quiet["finish_reason"] is None


def test_it_records_the_think_cap_break_and_the_followup() -> None:
    """A real stream arrives in small deltas, so the break lands ON the cap."""
    chunked = [Delta(reasoning="r" * 1_000) for _ in range(60)]
    t = FakeTransport([chunked, [Delta(content="fn f() {}")]],
                      spec=_spec(thinking_char_cap=40_000))

    meta = _run(t)[0]

    assert meta["stop"] == "think_cap"
    assert meta["followup"] is True
    assert meta["reasoning_chars"] == 40_000   # the break, not the full stream


def test_the_cap_is_a_floor_on_what_was_received_not_a_ceiling() -> None:
    """A delta cannot be un-received: `_consume` appends it and then tests the
    running total, so one oversized delta is captured whole and the recorded
    length exceeds the cap. Pinned because the reasoning_chars figure is used to
    size the cap, and reading it as 'never more than the cap' would hide exactly
    the tail the cap exists for."""
    t = FakeTransport([[Delta(reasoning="r" * 60_000)], [Delta(content="x")]],
                      spec=_spec(thinking_char_cap=40_000))

    meta = _run(t)[0]

    assert meta["stop"] == "think_cap"
    assert meta["reasoning_chars"] == 60_000


def test_it_records_server_prompt_tokens_and_the_request_budget() -> None:
    """`prompt_tokens` is what drives `adaptive_cpt`; it was read and discarded.
    `request_max` is the denominator a `finish_reason=length` is relative to."""
    t = FakeTransport([[Delta(content="x")]], prompt_tokens=[4321])

    meta = _run(t)[0]

    assert meta["prompt_tokens"] == 4321
    assert isinstance(meta["request_max"], int) and meta["request_max"] > 0
    assert meta["enable_thinking"] is True


def test_a_healthy_call_records_no_stop_and_no_followup() -> None:
    meta = _run(FakeTransport([[Delta(reasoning="r" * 9_000),
                                Delta(content="fn f() {}")]],
                              spec=_spec(thinking_char_cap=40_000)))[0]

    assert meta["stop"] is None
    assert meta["followup"] is False
    assert meta["content_chars"] == len("fn f() {}")
    assert meta["reasoning_chars"] == 9_000
