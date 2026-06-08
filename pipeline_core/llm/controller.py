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


@dataclass(slots=True)
class Hooks:
    """I/O sinks injected from ``model_interact`` (kept out of the pure control logic)."""

    sock_open: Callable[[str], None] = field(default=_noop_label)
    sock_write: Callable[[str], None] = field(default=_noop_text)
    sock_close: Callable[[], None] = field(default=_noop_close)
    guard: Callable[[str, str], None] = field(default=_noop_guard)
    dbg: Callable[[str], None] = field(default=_noop_dbg)
    persist_thinking: Callable[[str, str], None] = field(default=_noop_persist)


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
    ) -> str:
        spec = transport.spec()
        state = transport.budget_state()

        messages: list[dict] = []
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

        meta = StreamMeta()
        cap: _Captured | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self._h.sock_open(f"{transport.served_model} think={enable_thinking} attempt={attempt}")
            meta = StreamMeta()
            try:
                cap = self._consume(
                    transport.stream(messages, model=model, max_tokens=plan.request_max,
                                     enable_thinking=enable_thinking, meta=meta),
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

        # Thinking exhausted (no content) or stream length-truncated mid-output:
        # re-feed the reasoning and ask for code only, thinking disabled.
        followup = (transport.supports_followup and reasoning_text
                    and (not result or meta.finish_reason == "length"))
        if followup:
            result = self._followup(transport, messages, reasoning_text, model, max_tokens) or result

        if reasoning_text:
            self._h.persist_thinking(result, reasoning_text)

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

    def _followup(self, transport: Transport, messages: list[dict], reasoning_text: str,
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
        """Recompute the budget plan using the server's exact input-token count."""
        thinking_reserve = prev_plan.thinking_reserve
        content_cap = prev_plan.content_cap
        if spec.cap_by_ctx:
            safe = max(512, spec.ctx_len - real_tokens - thinking_reserve - spec.margin)
            content_cap = min(content_cap, safe)
        req = (content_cap + thinking_reserve) if spec.send_max_tokens else None
        return Plan(req, real_tokens, content_cap, thinking_reserve)
