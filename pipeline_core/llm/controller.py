"""The LLM control plane: one recovery/capping/retry policy for every transport.

``LLMController.run`` is the single implementation of everything that used to be
copy-pasted (and drift) across backends:

  * ctx-budget-aware ``max_tokens`` (via :mod:`.budget`) — no hard caps,
  * structural early-stop (via :class:`.StopDetector`),
  * a mid-stream reasoning cap that *recovers* rather than truncates,
  * the thinking-exhausted / length-truncated **followup** that re-feeds the
    model's own reasoning and asks for code only,
  * input-too-long (re-estimate + retry) and network (wait + retry) handling,
  * reasoning persistence + EMPTY_RESPONSE diagnostics.

Transports stay dumb: they stream normalized :class:`Delta`s and declare a
:class:`BudgetSpec`. They carry no policy, so adding/replacing a backend can never
again reintroduce a private cap or a missing followup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pipeline_core.domain.reasoning_cap import record_followup_meta, record_stream_meta
from pipeline_core.domain.text import PromptDigest

from .budget import BudgetSpec, BudgetState, Plan, request_max_tokens
from .detect import StopDetector
from .events import (
    Delta,
    InputTooLong,
    StreamMeta,
    Transport,
    TransportTimeout,
    TransportUnavailable,
)

_MAX_ATTEMPTS = 3


def _noop_label(label: str) -> None: ...
def _noop_text(text: str) -> None: ...
def _noop_close() -> None: ...
def _noop_guard(location: str, msg: str) -> None: ...
def _noop_dbg(msg: str) -> None: ...
def _noop_persist(result: str, thinking: str) -> None: ...
def _noop_emit(event: str, data: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class Hooks:
    """I/O sinks injected from ``model_interact`` (kept out of the pure control logic)."""

    sock_open: Callable[[str], None] = field(default=_noop_label)
    sock_write: Callable[[str], None] = field(default=_noop_text)
    sock_close: Callable[[], None] = field(default=_noop_close)
    guard: Callable[[str, str], None] = field(default=_noop_guard)
    dbg: Callable[[str], None] = field(default=_noop_dbg)
    persist_thinking: Callable[[str, str], None] = field(default=_noop_persist)
    # The forced followup's OWN reasoning, keyed on the same response sha as
    # `persist_thinking` but stored beside it, never over it. Measured
    # 2026-08-26 on run `82157-476ddc16`: 754 followups had no trace on disk
    # at all — `persist_thinking` keeps the first writer of a sha, which is
    # the main trace, so the reasoning that actually wrote the reply on
    # record was discarded on every one of them.
    persist_followup_thinking: Callable[[str, str], None] = field(default=_noop_persist)
    # Structured vLLM-runtime events for the monitor (replaces guard-log scrapes).
    emit: Callable[[str, dict[str, Any]], None] = field(default=_noop_emit)


@dataclass(slots=True)
class _Captured:
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    stop: str | None = None        # "block" / "file" / "think_cap" / None
    saw_content: bool = False


@dataclass(frozen=True, slots=True)
class _FollowupOutcome:
    """What the forced followup produced: its cleaned reply and its OWN
    reasoning (distinct from the main trace it was re-fed)."""

    result: str = ""
    reasoning: str = ""


def refed_reasoning_chars(reasoning_text: str, base_messages: list[dict[str, Any]],
                          fu_messages: list[dict[str, Any]]) -> int:
    """How many chars of the main trace the followup request actually carried.

    MEASURED on the turns the transport appended, never assumed from its trim
    rule: every `followup_messages` here re-feeds a SUFFIX of the trace (whole,
    or trimmed from the front to keep the conclusion — vLLM by ctx arithmetic,
    Ollama-compat/SGLang at 6 000 chars, the native-Ollama path in an
    assistant `thinking` field), and the base transport re-feeds nothing. So
    the answer is the longest suffix of `reasoning_text` that appears verbatim
    in the appended turns' string fields. Suffix-containment is monotone (a
    contained suffix contains every shorter suffix), which is what makes the
    binary search below exact rather than heuristic. A transport that re-fed
    nothing reads 0, which is the honest figure — the 2026-08-09 assistant-turn
    bug (33 prompt tokens whatever the trace size) would have read 0 here."""
    appended = fu_messages[len(base_messages):]
    haystack = "\n".join(v for m in appended for v in m.values() if isinstance(v, str))
    n = len(reasoning_text)
    if not n or not haystack:
        return 0
    lo, hi = 0, n            # invariant: suffix of length `lo` is contained
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if reasoning_text[n - mid:] in haystack:
            lo = mid
        else:
            hi = mid - 1
    return lo


class LLMController:
    def __init__(self, hooks: Hooks, *, default_sentinel: int) -> None:
        self._h = hooks
        self._sentinel = default_sentinel

    # -- public entry ------------------------------------------------------
    def run(
        self,
        transport: Transport,
        *,
        system: str | None,
        prompt: str,
        model: str,
        max_tokens: int,
        want_think: bool | None = None,
        temperature: float | None = None,
        followup_think: bool = False,
    ) -> str:
        """`followup_think` sizes and steers the forced followup (see
        `_followup`): False keeps the followup a code-only completion with no
        thinking reserve; True gives it the same reserve, rung and cap as the
        main stream. Default False so every existing caller is byte-identical;
        the escalated grade opts in at its own call site."""
        spec = transport.spec()
        state = transport.budget_state()

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        msg_chars = sum(len(m.get("content") or "") for m in messages)

        enable_thinking = transport.resolve_thinking(model, want_think)
        plan = request_max_tokens(
            spec, state,
            input_chars=msg_chars,
            caller_max_tokens=max_tokens,
            default_sentinel=self._sentinel,
            enable_thinking=enable_thinking,
        )
        # debt#7: announce the adaptive token budget so the monitor's budget bar is
        # live during this call (not just after a cpt update lands).
        self._h.emit("vllm.budget", {"request_max": plan.request_max})

        meta = StreamMeta()
        cap: _Captured | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self._h.sock_open(f"{transport.served_model} think={enable_thinking} attempt={attempt}")
            meta = StreamMeta()
            try:
                cap = self._consume(
                    transport.stream(messages, model=model, max_tokens=plan.request_max,
                                     enable_thinking=enable_thinking, meta=meta,
                                     temperature=temperature),
                    spec.thinking_char_cap, model,
                )
                break
            except TransportTimeout as exc:
                self._h.dbg(f"CHUNK_TIMEOUT model={model} | {exc}")
                self._h.sock_write(f"\n[TIMEOUT: {exc}]\n")
                self._h.sock_close()
                return ""
            except InputTooLong as exc:
                self._h.sock_close()
                if attempt >= _MAX_ATTEMPTS - 1 or exc.real_tokens is None:
                    raise
                prev, new = state.update(msg_chars, exc.real_tokens)
                # Re-estimate the budget directly from the server's reported input count.
                plan = self._replan_with_input(spec, state, exc.real_tokens, max_tokens,
                                               enable_thinking, plan)
                self._h.guard("vllm/400_retry",
                              f"attempt={attempt} real_input={exc.real_tokens}tok "
                              f"new_req_max={plan.request_max} cpt_ewm={prev:.2f}→{new:.2f}")
                self._h.emit("vllm.input_retry",
                             {"request_max": plan.request_max, "cpt": new})
                continue
            except TransportUnavailable as exc:
                self._h.sock_close()
                self._h.guard("llm/network_retry", f"attempt={attempt} err={type(exc).__name__}: {exc}")
                if attempt >= _MAX_ATTEMPTS - 1:
                    raise
                transport.wait_until_ready()
                continue

        assert cap is not None
        result = transport.clean("".join(cap.content), model=model)
        reasoning_text = "".join(cap.reasoning)

        if cap.stop == "think_cap":
            self._h.guard("llm/think_cap",
                          f"model={model} broke stream at {len(reasoning_text)} thinking chars")
        if meta.finish_reason == "length":
            self._h.guard("llm/length_truncated",
                          f"model={model} hit max_tokens={plan.request_max} "
                          f"(content={len(result)} chars, thinking={len(reasoning_text)} chars)")

        # Adaptive chars-per-token from the server's real usage.
        if spec.adaptive_cpt and meta.prompt_tokens:
            prev, new = state.update(msg_chars, meta.prompt_tokens)
            self._h.guard("vllm/cpt_update",
                          f"actual={meta.prompt_tokens}tok est={plan.est_input_tokens}tok "
                          f"ewm={prev:.2f}→{new:.2f}")
            self._h.emit("vllm.cpt_update", {"cpt": new, "request_max": plan.request_max})

        # Thinking exhausted (no content) or stream length-truncated mid-output:
        # re-feed the reasoning and ask for code only, thinking disabled.
        #
        # `cap.stop == "think_cap"` is the third arm and it is not redundant.
        # Capping a stream converts a server-side `finish_reason == "length"`
        # into a CLIENT-side break, and a client-side break abandons the
        # generator before the chunk carrying finish_reason ever arrives — so
        # `meta.finish_reason` stays None on exactly the path the cap creates.
        # The `not result` arm covers it whenever the break lands before any
        # content, which on vLLM is every time in practice (the cap test in
        # `_consume` sits inside `if delta.reasoning`, and Qwen3.6 emits its
        # whole reasoning block before the first content delta). The arm is here
        # for the case that ordering does not hold: break after content has
        # started and `result` is non-empty while finish_reason is None, so the
        # first two arms both miss and a TRUNCATED body would be returned as
        # though it were complete.
        followup = (transport.supports_followup and reasoning_text
                    and (not result or meta.finish_reason == "length"
                         or cap.stop == "think_cap"))
        prompt_sha = PromptDigest.of(prompt).sha256
        fu = _FollowupOutcome()
        if followup:
            fu = self._followup(transport, messages, reasoning_text, model, max_tokens,
                                prompt_sha=prompt_sha, followup_think=followup_think)
            result = fu.result or result

        # Both traces are keyed on the sha of the string this call RETURNS —
        # the one `llm.response` records — so `logs/thinking/<sha>.txt` and
        # `<sha>.followup.txt` are siblings a reader reaches from one event.
        # Keying the followup trace on the followup's own (possibly empty)
        # reply would strand it whenever `fu.result` was "" and the main
        # result stood.
        if reasoning_text:
            self._h.persist_thinking(result, reasoning_text)
        if fu.reasoning:
            self._h.persist_followup_thinking(result, fu.reasoning)

        # Stream provenance. `llm.response` (adapters/ask_client.py) records only
        # `{prompt_sha, response: {sha256, char_count}}`, and StreamMeta cannot
        # reach it: this method returns a bare `str`, and so do
        # `model_interact.ask` and the late-binding `ask_raw` the adapter holds.
        # So the facts live here, on a sibling event, keyed on the SAME
        # `PromptDigest.of(prompt).sha256` the adapter uses — identical input,
        # identical function, so the two join exactly.
        #
        # Why it is worth an event of its own: diagnosing the 2026-08-08 slow run
        # was impossible from `events.jsonl` and needed `logs/pipeline_guards.log`
        # (which carries no run ids and no dates, and spans months) plus the vLLM
        # Prometheus endpoint. A `char_count: 0` response is indistinguishable
        # there from a short one, so four calls that each burned ~20 minutes of
        # GPU producing zero characters looked like ordinary slow calls.
        self._h.emit("llm.stream_meta", {
            "prompt_sha": prompt_sha,
            "model": model,
            "finish_reason": meta.finish_reason,
            "prompt_tokens": meta.prompt_tokens,
            "content_chars": len(result),
            "reasoning_chars": len(reasoning_text),
            "stop": cap.stop,
            "followup": bool(followup),
            "request_max": plan.request_max,
            "enable_thinking": enable_thinking,
        })
        record_stream_meta({
            "prompt_sha": prompt_sha,
            "model": model,
            "finish_reason": meta.finish_reason,
            "prompt_tokens": meta.prompt_tokens,
            "content_chars": len(result),
            "reasoning_chars": len(reasoning_text),
            "stop": cap.stop,
            "followup": bool(followup),
            "request_max": plan.request_max,
            "enable_thinking": enable_thinking,
        })

        self._h.sock_close()
        if not result:
            self._h.dbg(f"EMPTY_RESPONSE model={model} input_chars={msg_chars} "
                        f"thinking_chars={len(reasoning_text)}")
        return result

    # -- internals ---------------------------------------------------------
    def _consume(self, stream: Iterator[Delta], thinking_char_cap: int | None,
                 model: str) -> _Captured:
        cap = _Captured()
        detector = StopDetector()
        think_chars = 0
        for delta in stream:
            if delta.reasoning:
                cap.reasoning.append(delta.reasoning)
                self._h.sock_write(delta.reasoning)
                think_chars += len(delta.reasoning)
                if thinking_char_cap and think_chars >= thinking_char_cap:
                    self._h.sock_write(f"\n[THINK_CAP: {thinking_char_cap} chars]\n")
                    cap.stop = "think_cap"
                    break
            if delta.content:
                if not cap.saw_content:
                    self._h.sock_write("\n[CONTENT]\n")
                    cap.saw_content = True
                cap.content.append(delta.content)
                self._h.sock_write(delta.content)
                if detector.feed(delta.content):
                    marker = "BLOCK_CLOSED" if detector.reason == "block" else "FILE_CLOSED"
                    self._h.sock_write(f"\n[{marker}]\n")
                    self._h.guard(f"llm/{detector.reason}_closed",
                                  f"model={model} structure closed at {detector.chars} chars")
                    cap.stop = "closed"
                    break
        return cap

    def _followup(self, transport: Transport, messages: list[dict[str, Any]], reasoning_text: str,
                  model: str, max_tokens: int, *, prompt_sha: str,
                  followup_think: bool) -> _FollowupOutcome:
        """The forced code-only completion after a cut main stream.

        `followup_think` is the one lever this turn has. With it False (every
        caller until 2026-08-26) the followup is planned with NO thinking
        reserve and streamed with thinking disabled — which on the strength
        dialect is not "off" but the lowest rung, so the model reasons anyway,
        inside a `request_max` that is the content budget alone, with no cap
        on that reasoning. Measured on run `82157-476ddc16`: the escalated
        constructor call (`49385be5…`) ran its main stream to `length` at
        71 584 reasoning chars, and the 8 192-char reply on record was this
        followup's (`[llm/followup] thinking_len=71584 result_len=8192`);
        record-wide, 1 595 of 1 596 followup replies. With it True the
        followup gets the same reserve, rung and mid-stream cap as the main
        stream — a second full attempt that has the first attempt's trace.

        The followup's own facts leave on `llm.followup_meta`, keyed on the
        ORIGINAL prompt's sha so it joins `llm.request` / `llm.response` /
        `llm.stream_meta` exactly. Until this event existed the stream_meta
        row carried the MAIN stream's `finish_reason` beside the FOLLOWUP's
        `content_chars`, and a followup cut at `length` was unrecordable."""
        spec = transport.spec()
        state = transport.budget_state()
        fu_messages = transport.followup_messages(messages, reasoning_text, model=model)
        fu_chars = sum(len(m.get("content") or "") for m in fu_messages)
        fu_plan = request_max_tokens(
            spec, state, input_chars=fu_chars, caller_max_tokens=max_tokens,
            default_sentinel=self._sentinel, enable_thinking=followup_think,
        )
        self._h.dbg(f"FOLLOWUP model={model} thinking_len={len(reasoning_text)} "
                    f"fu_max={fu_plan.request_max} think={followup_think}")
        self._h.sock_open(f"{transport.served_model}-followup think={followup_think}")
        self._h.sock_write("\n[FOLLOWUP — thinking budget exhausted, requesting code]\n")
        meta = StreamMeta()
        try:
            cap = self._consume(
                transport.stream(fu_messages, model=model, max_tokens=fu_plan.request_max,
                                 enable_thinking=followup_think, meta=meta),
                spec.thinking_char_cap if followup_think else None, model,
            )
        except (TransportTimeout, TransportUnavailable) as exc:
            self._h.guard("llm/followup_network", f"err={type(exc).__name__}: {exc}")
            return _FollowupOutcome()
        result = transport.clean("".join(cap.content), model=model)
        fu_reasoning = "".join(cap.reasoning)
        self._h.guard("llm/followup", f"model={model} thinking_len={len(reasoning_text)} result_len={len(result)}")
        fu_meta = {
            "prompt_sha": prompt_sha,
            "model": model,
            "finish_reason": meta.finish_reason,
            "content_chars": len(result),
            "reasoning_chars": len(fu_reasoning),
            "reasoning_chars_refed": refed_reasoning_chars(reasoning_text, messages, fu_messages),
            "request_max": fu_plan.request_max,
            "enable_thinking": followup_think,
            "stop": cap.stop,
            "fn_blocks": result.count("<FN "),
        }
        self._h.emit("llm.followup_meta", fu_meta)
        record_followup_meta(fu_meta)
        return _FollowupOutcome(result=result, reasoning=fu_reasoning)

    @staticmethod
    def _replan_with_input(spec: BudgetSpec, state: BudgetState, real_tokens: int,
                           max_tokens: int, enable_thinking: bool, prev_plan: Plan) -> Plan:
        """Recompute the budget plan from the server's reported input-token
        count. The server reports "at least N" — a LOWER bound, not exact;
        treating it as exact left the retry ~1 token over ctx on every
        attempt (anyhow 2026-07-02: 25365+40172 = 65537 > 65536).
        Pad it before capping so the retry actually fits."""
        thinking_reserve = prev_plan.thinking_reserve
        content_cap = prev_plan.content_cap
        padded_input = int(real_tokens * 1.02) + 256
        if spec.cap_by_ctx:
            safe = max(512, spec.ctx_len - padded_input - thinking_reserve - spec.margin)
            content_cap = min(content_cap, safe)
        req = (content_cap + thinking_reserve) if spec.send_max_tokens else None
        return Plan(req, real_tokens, content_cap, thinking_reserve)
