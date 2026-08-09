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
    # debt#7: structured vLLM-runtime events for the monitor (replaces guard-log scrapes).
    emit: Callable[[str, dict[str, Any]], None] = field(default=_noop_emit)


@dataclass(slots=True)
class _Captured:
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    stop: str | None = None        # "block" / "file" / "think_cap" / None
    saw_content: bool = False


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
    ) -> str:
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
        if followup:
            result = self._followup(transport, messages, reasoning_text, model, max_tokens) or result

        if reasoning_text:
            self._h.persist_thinking(result, reasoning_text)

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
            "prompt_sha": PromptDigest.of(prompt).sha256,
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
                  model: str, max_tokens: int) -> str:
        spec = transport.spec()
        state = transport.budget_state()
        fu_messages = transport.followup_messages(messages, reasoning_text, model=model)
        fu_chars = sum(len(m.get("content") or "") for m in fu_messages)
        fu_plan = request_max_tokens(
            spec, state, input_chars=fu_chars, caller_max_tokens=max_tokens,
            default_sentinel=self._sentinel, enable_thinking=False,
        )
        self._h.dbg(f"FOLLOWUP model={model} thinking_len={len(reasoning_text)} fu_max={fu_plan.request_max}")
        self._h.sock_open(f"{transport.served_model}-followup think=False")
        self._h.sock_write("\n[FOLLOWUP — thinking budget exhausted, requesting code]\n")
        meta = StreamMeta()
        try:
            cap = self._consume(
                transport.stream(fu_messages, model=model, max_tokens=fu_plan.request_max,
                                 enable_thinking=False, meta=meta),
                None, model,
            )
        except (TransportTimeout, TransportUnavailable) as exc:
            self._h.guard("llm/followup_network", f"err={type(exc).__name__}: {exc}")
            return ""
        result = transport.clean("".join(cap.content), model=model)
        self._h.guard("llm/followup", f"model={model} thinking_len={len(reasoning_text)} result_len={len(result)}")
        return result

    @staticmethod
    def _replan_with_input(spec: BudgetSpec, state: BudgetState, real_tokens: int,
                           max_tokens: int, enable_thinking: bool, prev_plan: Plan) -> Plan:
        """Recompute the budget plan from the server's reported input-token
        count. The server reports "at least N" — a LOWER bound, not exact;
        treating it as exact left the retry ~1 token over ctx on every
        attempt (debt #32, anyhow 2026-07-02: 25365+40172 = 65537 > 65536).
        Pad it before capping so the retry actually fits."""
        thinking_reserve = prev_plan.thinking_reserve
        content_cap = prev_plan.content_cap
        padded_input = int(real_tokens * 1.02) + 256
        if spec.cap_by_ctx:
            safe = max(512, spec.ctx_len - padded_input - thinking_reserve - spec.margin)
            content_cap = min(content_cap, safe)
        req = (content_cap + thinking_reserve) if spec.send_max_tokens else None
        return Plan(req, real_tokens, content_cap, thinking_reserve)
