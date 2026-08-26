"""`llm.followup_meta` — the forced followup's OWN record — and the lever that
gives the escalated grade's followup a thinking reserve.

Measured 2026-08-26 on run `82157-476ddc16` (`scripts/followup_completion_audit.py`):
the escalated constructor call `49385be5…` ran its main stream to
`finish_reason=length` at 71 584 reasoning chars under `request_max 16384`;
the reply on record (`content_chars 8192`) was the FOLLOWUP's — guard line
`[llm/followup] thinking_len=71584 result_len=8192` — written with
`enable_thinking=False`, which on the strength dialect is the lowest rung that
still reasons, under `request_max` = the content budget (8 192) with no
thinking reserve and no reasoning cap. 8 of 8 file-first replies in that run,
1 595 of 1 596 record-wide. The `llm.stream_meta` row carried the MAIN
stream's `finish_reason` beside the FOLLOWUP's `content_chars`, and 754
followups left no trace on disk. Every assertion below is one of those facts
made recordable, or the lever that changes it, each with its negative control.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.reasoning_cap import (
    collect_stream_meta,
    summarize_capped,
    summarize_followup_cut,
)
from pipeline_core.domain.text import PromptDigest
from pipeline_core.llm import Delta, Hooks, LLMController
from pipeline_core.llm.controller import refed_reasoning_chars
from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

PROMPT = "fill src/error.rs::construct_from_display"
# The live shape: VLLM_CONTENT_BUDGET=8192, VLLM_THINKING_BUDGET=16384.
LIVE_CONTENT, LIVE_THINKING = 8192, 16384


def _run(transport: FakeTransport, *, followup_think: bool = False,
         hooks: Hooks | None = None) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    hooks = hooks or Hooks()
    hooks.emit = lambda name, data: events.setdefault(name, []).append(dict(data))
    ctl = LLMController(hooks, default_sentinel=SENTINEL)
    out = ctl.run(transport, system="sys", prompt=PROMPT, model="m", max_tokens=SENTINEL,
                  followup_think=followup_think)
    return out, events


def _live_spec(**kw: Any):
    return _spec(content_budget=LIVE_CONTENT, thinking_budget=LIVE_THINKING, **kw)


# --------------------------------------------------------------------------- #
# (a) the followup's row says what the FOLLOWUP did; the stream row keeps the main's
# --------------------------------------------------------------------------- #
def test_followup_meta_carries_the_followups_finish_reason_not_the_mains() -> None:
    t = FakeTransport(
        [[Delta(reasoning="r" * 71_584)], [Delta(content="<FN a>x</FN>")]],
        spec=_live_spec(), finish_reasons=["length", "stop"])

    _, events = _run(t)

    stream, fu = events["llm.stream_meta"], events["llm.followup_meta"]
    assert len(stream) == 1 and len(fu) == 1
    assert stream[0]["finish_reason"] == "length" and stream[0]["followup"] is True
    assert fu[0]["finish_reason"] == "stop"
    # Same key as llm.request / llm.response / llm.stream_meta: the ORIGINAL prompt.
    assert fu[0]["prompt_sha"] == stream[0]["prompt_sha"] == PromptDigest.of(PROMPT).sha256
    assert fu[0]["content_chars"] == stream[0]["content_chars"] == len("<FN a>x</FN>")
    assert fu[0]["fn_blocks"] == 1
    assert fu[0]["model"] == "m"


def test_a_followup_cut_at_length_is_recorded_with_its_own_reasoning() -> None:
    """The unrecordable case before the event: the completion that wrote the
    reply on record was itself truncated. `reasoning_chars` is the followup's
    OWN reasoning (the strength dialect reasons on the low rung too), never
    the re-fed trace, which is `reasoning_chars_refed`."""
    t = FakeTransport(
        [[Delta(reasoning="r" * 100)], [Delta(reasoning="s" * 50), Delta(content="partial")]],
        spec=_live_spec(), finish_reasons=["length", "length"])

    _, events = _run(t)

    fu = events["llm.followup_meta"][0]
    assert fu["finish_reason"] == "length"
    assert fu["reasoning_chars"] == 50
    assert fu["reasoning_chars_refed"] == 100     # FakeTransport re-feeds the whole trace
    assert fu["content_chars"] == len("partial")
    assert fu["fn_blocks"] == 0


# --------------------------------------------------------------------------- #
# (b) NEGATIVE CONTROL — no followup, no followup row
# --------------------------------------------------------------------------- #
def test_a_call_that_needed_no_followup_emits_no_followup_meta() -> None:
    t = FakeTransport([[Delta(reasoning="r"), Delta(content="fn f() {}")]],
                      spec=_live_spec(), finish_reasons=["stop"])

    _, events = _run(t)

    assert "llm.followup_meta" not in events
    assert events["llm.stream_meta"][0]["followup"] is False
    assert len(t.calls) == 1


# --------------------------------------------------------------------------- #
# (c) the lever: followup_think sizes the reserve and picks the rung
# --------------------------------------------------------------------------- #
def _followup_under(flag: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    t = FakeTransport([[Delta(reasoning="r" * 100)], [Delta(content="x")]],
                      spec=_live_spec(), finish_reasons=["length", "stop"])
    _, events = _run(t, followup_think=flag)
    return t.calls[1], events["llm.followup_meta"][0]


def test_followup_think_false_is_todays_followup_content_budget_and_thinking_off() -> None:
    """NEGATIVE CONTROL for the lever: the default reproduces `82157`'s
    followup exactly — `request_max` = the content budget alone, thinking
    disabled (the low rung on the strength dialect)."""
    call, fu = _followup_under(False)

    assert call["max_tokens"] == LIVE_CONTENT and call["enable_thinking"] is False
    assert fu["request_max"] == LIVE_CONTENT and fu["enable_thinking"] is False


def test_followup_think_true_adds_the_thinking_reserve_and_the_on_rung() -> None:
    call, fu = _followup_under(True)

    assert call["max_tokens"] == LIVE_CONTENT + LIVE_THINKING and call["enable_thinking"] is True
    assert fu["request_max"] == LIVE_CONTENT + LIVE_THINKING and fu["enable_thinking"] is True


def test_the_reasoning_cap_governs_the_followup_only_when_it_thinks() -> None:
    """A followup given the main stream's reserve can run away like the main
    stream, so it gets the main stream's cap. Without the reserve there is
    nothing to cap: the request bound is the cap, as today."""
    spec = _live_spec(thinking_char_cap=10)

    def run(flag: bool) -> dict[str, Any]:
        t = FakeTransport([[Delta(reasoning="r" * 5)],
                           [Delta(reasoning="s" * 50), Delta(content="code")]],
                          spec=spec, finish_reasons=["length", "stop"])
        return _run(t, followup_think=flag)[1]["llm.followup_meta"][0]

    off = run(False)
    assert off["stop"] is None and off["content_chars"] == len("code")
    on = run(True)
    assert on["stop"] == "think_cap" and on["content_chars"] == 0


# --------------------------------------------------------------------------- #
# (d) the followup's own trace reaches its own hook, keyed on the returned reply
# --------------------------------------------------------------------------- #
def _trace_hooks() -> tuple[Hooks, dict[str, tuple[str, str]]]:
    seen: dict[str, tuple[str, str]] = {}
    return Hooks(persist_thinking=lambda r, th: seen.update(main=(r, th)),
                 persist_followup_thinking=lambda r, th: seen.update(fu=(r, th))), seen


def test_followup_reasoning_reaches_the_followup_hook_beside_the_main_trace() -> None:
    hooks, seen = _trace_hooks()
    t = FakeTransport([[Delta(reasoning="main trace")],
                       [Delta(reasoning="followup trace"), Delta(content="code")]],
                      spec=_live_spec(), finish_reasons=["length", "stop"])

    out, _ = _run(t, hooks=hooks)

    assert out == "code"
    assert seen["main"] == ("code", "main trace")
    assert seen["fu"] == ("code", "followup trace")


def test_followup_trace_is_keyed_on_the_reply_the_call_returns() -> None:
    """When the followup produced no content the main reply stands and is what
    `llm.response` records; the followup trace must sit under THAT sha or a
    reader following the event finds nothing."""
    hooks, seen = _trace_hooks()
    t = FakeTransport([[Delta(reasoning="m"), Delta(content="partial")],
                       [Delta(reasoning="f")]],
                      spec=_live_spec(), finish_reasons=["length", "stop"])

    out, _ = _run(t, hooks=hooks)

    assert out == "partial"
    assert seen["fu"] == ("partial", "f")


def test_a_followup_that_reasoned_nothing_persists_no_followup_trace() -> None:
    """NEGATIVE CONTROL: the boolean dialect's `enable_thinking=False` really
    is off — no reasoning, no file."""
    hooks, seen = _trace_hooks()
    t = FakeTransport([[Delta(reasoning="main trace")], [Delta(content="code")]],
                      spec=_live_spec(), finish_reasons=["length", "stop"])

    _run(t, hooks=hooks)

    assert "fu" not in seen and seen["main"] == ("code", "main trace")


# --------------------------------------------------------------------------- #
# reasoning_chars_refed is measured on the request, not assumed from a trim rule
# --------------------------------------------------------------------------- #
def test_refed_chars_are_the_longest_suffix_carried_by_the_appended_turns() -> None:
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "p"}]
    trace = "".join(f"{i:04d} " for i in range(400))      # 2 000 chars, no repeats

    # vLLM / compat shape: a front-trimmed tail inside the fixed user-turn wrapper.
    fu = base + [{"role": "user", "content": (
        f"Your earlier reasoning on this exact task:\n\n{trace[-250:]}\n\n"
        "Now write the code block. Do not re-explain — output only the code.")}]
    assert refed_reasoning_chars(trace, base, fu) == 250

    # native-Ollama shape: the whole trace in an assistant `thinking` field.
    fu = base + [{"role": "assistant", "thinking": trace, "content": ""},
                 {"role": "user", "content": "code only"}]
    assert refed_reasoning_chars(trace, base, fu) == len(trace)

    # NEGATIVE CONTROL: the base transport appends nothing — and so did the
    # 2026-08-09 assistant `<think>` turn the template discarded (33 prompt
    # tokens whatever the size). Both read 0, which is the truth.
    assert refed_reasoning_chars(trace, base, base) == 0
    assert refed_reasoning_chars("", base, fu) == 0


# --------------------------------------------------------------------------- #
# the audit window sees both rows and folds them separately
# --------------------------------------------------------------------------- #
def test_the_fill_window_folds_the_followup_cut_without_inflating_the_reply_total() -> None:
    t = FakeTransport([[Delta(reasoning="r" * 100)], [Delta(content="x")]],
                      spec=_live_spec(), finish_reasons=["length", "length"])

    with collect_stream_meta() as rows:
        _run(t)
        assert summarize_capped(rows) == (1, 1)     # one reply, capped — not (2, 2)
        assert summarize_followup_cut(rows) == 1
