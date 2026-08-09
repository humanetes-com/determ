"""The vLLM reasoning cap, and the three things it must not break.

Until 2026-08-09 `VLLMTransport.spec()` set `thinking_char_cap=None` while
`OllamaTransport.spec()` set 40,000, and `_consume` only breaks a reasoning
stream when the cap is set — so nothing interrupted a vLLM stream. Four calls in
run `491052-1af6b4f4` each burned the whole 32,768-token budget inside the
reasoning block and returned ZERO characters of answer:

    [18:07:30] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,481)
    [20:36:53] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,109)
    [22:03:22] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,432)
    [22:44:43] llm/length_truncated hit max_tokens=32768 (content=0, thinking=124,043)

856 + 872 + 1,364 + 856 seconds, 65.8 minutes of a 464-minute run.

Three tests, one per hazard the change carries:
  * the negative control — the runaway must reproduce with the cap off, or
    nothing below is evidence about the live system;
  * no re-coupling — the May 2026 failure was a cap wired into `max_tokens`,
    which predicted a token count and returned HTTP 400 when the prediction was
    wrong. `request_max_tokens` must be invariant under the cap;
  * the followup edge — capping converts a server-side `finish_reason="length"`
    into a client-side break, and the recovery keyed on the former.
"""
from __future__ import annotations

from collections.abc import Iterator

from pipeline_core.adapters.llm_transports import (
    VLLM_THINK_EMPTY_BAND,
    VLLMTransport,
)
from pipeline_core.llm import BudgetState, Delta, Hooks, LLMController, StreamMeta
from pipeline_core.llm.budget import request_max_tokens
from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

# The observed shape: one runaway trace, reasoning only, no content ever.
RUNAWAY_CHARS = 121_481
CAP = 40_000


def _runaway(n_chars: int = RUNAWAY_CHARS) -> list[Delta]:
    """A reasoning stream in 1,000-char deltas and not one content delta —
    the 18:07:30 call."""
    return [Delta(reasoning="r" * 1_000) for _ in range(n_chars // 1_000)]


def _controller_with_guards() -> tuple[LLMController, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []
    hooks = Hooks(guard=lambda name, msg: seen.append((name, msg)))
    return LLMController(hooks, default_sentinel=SENTINEL), seen


def _run(transport: FakeTransport) -> tuple[str, list[tuple[str, str]]]:
    ctl, seen = _controller_with_guards()
    out = ctl.run(transport, system=None, prompt="fill this fn", model="m",
                  max_tokens=SENTINEL)
    return out, seen


# --------------------------------------------------------------------------- #
# 1. negative control
# --------------------------------------------------------------------------- #
def test_uncapped_reproduces_the_121k_runaway() -> None:
    """NEGATIVE CONTROL. `thinking_char_cap=None` is the shipped-until-08-09
    vLLM config; the stream must run to exhaustion and yield no content, with
    `llm/length_truncated` naming content=0. If this stops failing to produce
    content, the fixture no longer describes the live pathology and the test
    below proves nothing."""
    t = FakeTransport([_runaway(), []], spec=_spec(thinking_char_cap=None),
                      finish_reasons=["length", None])

    out, guards = _run(t)

    assert out == ""
    assert any(n == "llm/length_truncated" and "content=0 chars" in m
               for n, m in guards), guards
    assert not any(n == "llm/think_cap" for n, _ in guards)
    # The whole stream was consumed: nothing interrupted it.
    assert len("".join(d.reasoning for d in _runaway())) == 121_000


def test_the_cap_breaks_the_same_stream_early() -> None:
    t = FakeTransport([_runaway(), [Delta(content="fn f() {}")]],
                      spec=_spec(thinking_char_cap=CAP),
                      finish_reasons=[None, None])

    out, guards = _run(t)

    caps = [m for n, m in guards if n == "llm/think_cap"]
    assert len(caps) == 1, guards
    assert f"broke stream at {CAP}" in caps[0]
    assert not any(n == "llm/length_truncated" for n, _ in guards)
    assert out == "fn f() {}"       # the followup supplied the answer


# --------------------------------------------------------------------------- #
# 2. no re-coupling with the token budget (the May 2026 bug)
# --------------------------------------------------------------------------- #
def test_request_max_tokens_is_invariant_under_the_cap() -> None:
    """The May 2026 failure was `safe_max_tokens` computed from a PREDICTED
    token count; it returned HTTP 400 on `input(57320) + max_tokens(40985) >
    ctx(98304)`. `thinking_char_cap` predicts nothing — it sums deltas already
    received — and it must never reach the request. Any coupling here would put
    the two back on the same wire."""
    common = {"input_chars": 100_000, "caller_max_tokens": SENTINEL,
              "default_sentinel": SENTINEL, "enable_thinking": True}
    plans = [
        request_max_tokens(_spec(thinking_char_cap=c), BudgetState(), **common)
        for c in (None, 1, CAP, 10**9)
    ]

    assert len({p.request_max for p in plans}) == 1, \
        f"request_max moved with the cap: {[p.request_max for p in plans]}"
    assert len({p.thinking_reserve for p in plans}) == 1
    assert len({p.content_cap for p in plans}) == 1


def test_breaking_early_can_only_reduce_tokens_used() -> None:
    """The error is one-sided by construction, which is why a wrong cap value
    costs answer quality and can never cost an HTTP 400."""
    capped = FakeTransport([_runaway(), [Delta(content="x")]],
                           spec=_spec(thinking_char_cap=CAP))
    uncapped = FakeTransport([_runaway(), []], spec=_spec(thinking_char_cap=None),
                             finish_reasons=["length", None])
    _run(capped)
    _run(uncapped)

    assert capped.calls[0]["max_tokens"] == uncapped.calls[0]["max_tokens"]


# --------------------------------------------------------------------------- #
# 3. the followup edge the cap creates
# --------------------------------------------------------------------------- #
def test_cap_with_no_content_still_fires_the_followup() -> None:
    """The common path: the cap test lives inside `if delta.reasoning`, and
    Qwen3.6 emits its whole reasoning block before any content, so the break
    lands with `result` empty and the `not result` arm carries it."""
    t = FakeTransport([_runaway(), [Delta(content="fn f() {}")]],
                      spec=_spec(thinking_char_cap=CAP))

    out, _ = _run(t)

    assert len(t.calls) == 2, "no followup fired"
    assert t.calls[1]["enable_thinking"] is False
    assert out == "fn f() {}"


def test_cap_after_content_started_still_fires_the_followup() -> None:
    """The edge, and the reason the `cap.stop` arm exists. Reasoning resuming
    after content means the break happens with `result` NON-empty, and a
    client-side break abandons the generator before the chunk carrying
    `finish_reason` — so it stays None. Both original arms miss, and without the
    third the truncated body is returned as though it were complete."""
    interleaved = [Delta(reasoning="r" * 30_000),
                   Delta(content="fn f() { // half"),
                   *[Delta(reasoning="r" * 1_000) for _ in range(15)]]
    t = FakeTransport([interleaved, [Delta(content="fn f() { done }")]],
                      spec=_spec(thinking_char_cap=CAP),
                      finish_reasons=[None, None])

    out, guards = _run(t)

    assert any(n == "llm/think_cap" for n, _ in guards)
    assert len(t.calls) == 2, "followup did NOT fire on a truncated body"
    assert out == "fn f() { done }"


def test_a_clean_short_reasoning_call_is_untouched(caplog) -> None:
    """The median call must not change. Server mean is 5,205 output tokens per
    request and healthy probe reasoning ran 7,000-12,000 chars — an order of
    magnitude under the cap."""
    t = FakeTransport([[Delta(reasoning="r" * 9_000), Delta(content="fn f() {}")]],
                      spec=_spec(thinking_char_cap=CAP))

    out, guards = _run(t)

    assert out == "fn f() {}"
    assert len(t.calls) == 1, "a healthy call fired a followup"
    assert not any(n in ("llm/think_cap", "llm/length_truncated") for n, _ in guards)


# --------------------------------------------------------------------------- #
# 4. the value itself
# --------------------------------------------------------------------------- #
def test_vllm_ships_a_cap_at_all() -> None:
    spec = VLLMTransport().spec()
    assert spec.thinking_char_cap is not None, \
        "vLLM is uncapped again — this is the 08-08 configuration"


def test_the_default_cap_lies_in_the_measured_empty_band() -> None:
    """Across 226 traces the reasoning-length distribution is bimodal: 0-36,570
    and 69,830-152,088, with a verified empty band between. A cap inside it
    separates the two populations, so the exact value does not matter — which is
    the claim this test keeps true as the value moves."""
    low, high = VLLM_THINK_EMPTY_BAND
    cap = VLLMTransport().spec().thinking_char_cap
    assert cap is not None and low <= cap <= high, \
        f"cap {cap} is outside the measured empty band {low}-{high}: it now " \
        "either truncates healthy reasoning or misses the runaway population"


def test_consume_breaks_exactly_at_the_cap_not_after_the_stream() -> None:
    """Breaking on the running total of deltas ALREADY RECEIVED is the whole
    reason this cannot repeat the May 2026 overflow. Pin that it is a running
    sum, not a post-hoc check."""
    ctl, _ = _controller_with_guards()
    consumed = 0

    def counting_stream() -> Iterator[Delta]:
        nonlocal consumed
        for d in _runaway():
            consumed += 1
            yield d

    cap = ctl._consume(counting_stream(), CAP, "m")

    assert cap.stop == "think_cap"
    assert consumed == CAP // 1_000, \
        f"consumed {consumed} deltas for a {CAP}-char cap — not a running sum"
    assert len(_runaway()) > consumed


# --------------------------------------------------------------------------- #
# 5. against the REAL VLLMTransport.stream
#
# `FakeTransport` sets `meta.finish_reason` eagerly at the top of `stream()`,
# before yielding anything. The real transport sets it per-chunk, only when a
# chunk carries one, and the carrying chunk is last. That difference is the
# whole subject of the test below, so it cannot be asserted through the fake.
# --------------------------------------------------------------------------- #
class _StubDelta:
    def __init__(self, content=None, reasoning=None):
        self.content, self.reasoning = content, reasoning


class _StubChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta, self.finish_reason = delta, finish_reason


class _StubChunk:
    def __init__(self, choices, usage=None):
        self.choices, self.usage = choices, usage


class _StubStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.consumed = 0

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def __iter__(self):
        for c in self._chunks:
            self.consumed += 1
            yield c


def _stub_openai(monkeypatch, chunks) -> _StubStream:
    """Patch `openai.OpenAI` so `VLLMTransport.stream` runs its real body."""
    import openai

    stream = _StubStream(chunks)

    class _Completions:
        def create(self, **_kw):
            return stream

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

        def __init__(self, **_kw):
            pass

    monkeypatch.setattr(openai, "OpenAI", _Client)
    return stream


def test_real_stream_leaves_finish_reason_none_on_a_client_side_break(monkeypatch) -> None:
    """The fact the third followup arm rests on. cargo-style: the server would
    have reported `finish_reason="length"` on its LAST chunk, but breaking at
    the cap abandons the generator long before it, so `meta.finish_reason` stays
    None and the `== "length"` arm cannot fire. Asserted against the real
    transport so nobody 'simplifies' the arm away on the fake's behaviour."""
    chunks = [_StubChunk([_StubChoice(_StubDelta(reasoning="r" * 1_000))])
              for _ in range(RUNAWAY_CHARS // 1_000)]
    chunks.append(_StubChunk([_StubChoice(_StubDelta(), finish_reason="length")]))
    stream = _stub_openai(monkeypatch, chunks)

    meta = StreamMeta()
    ctl, _ = _controller_with_guards()
    captured = ctl._consume(
        VLLMTransport().stream([], model="m", max_tokens=SENTINEL,
                               enable_thinking=True, meta=meta),
        CAP, "m")

    assert captured.stop == "think_cap"
    assert meta.finish_reason is None, \
        "the finish_reason chunk was reached — the fixture is not a mid-stream break"
    assert stream.consumed == CAP // 1_000 < len(chunks)


def test_real_stream_uncapped_reaches_the_length_finish_reason(monkeypatch) -> None:
    """NEGATIVE CONTROL for the test above: with no cap the same chunk sequence
    runs to the end and DOES record `length`. Without this, 'finish_reason is
    None' would also pass on a fixture that never carried one."""
    chunks = [_StubChunk([_StubChoice(_StubDelta(reasoning="r" * 1_000))])
              for _ in range(RUNAWAY_CHARS // 1_000)]
    chunks.append(_StubChunk([_StubChoice(_StubDelta(), finish_reason="length")]))
    stream = _stub_openai(monkeypatch, chunks)

    meta = StreamMeta()
    ctl, _ = _controller_with_guards()
    captured = ctl._consume(
        VLLMTransport().stream([], model="m", max_tokens=SENTINEL,
                               enable_thinking=True, meta=meta),
        None, "m")

    assert captured.stop is None
    assert meta.finish_reason == "length"
    assert stream.consumed == len(chunks)


def test_real_stream_reads_both_reasoning_field_names(monkeypatch) -> None:
    """vLLM has shipped the field as `reasoning` and as `reasoning_content`; the
    transport reads either. The cap sums whatever that read returns, so a field
    rename would silently disable it rather than fail loudly."""
    class _ReasoningContentDelta:
        content = None
        reasoning_content = "r" * 1_000

    chunks = [_StubChunk([_StubChoice(_ReasoningContentDelta())])
              for _ in range(60)]
    _stub_openai(monkeypatch, chunks)

    ctl, _ = _controller_with_guards()
    captured = ctl._consume(
        VLLMTransport().stream([], model="m", max_tokens=SENTINEL,
                               enable_thinking=True, meta=StreamMeta()),
        CAP, "m")

    assert captured.stop == "think_cap"
