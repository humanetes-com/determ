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
from dataclasses import dataclass, field, replace
from typing import Any

from pipeline_core.domain.llm_output import salvage_complete_blocks
from pipeline_core.domain.reasoning_cap import (
    is_bookkeeping_call,
    record_followup_meta,
    record_stream_meta,
)
from pipeline_core.domain.text import PromptDigest, turns_digest_text

from .budget import BudgetSpec, BudgetState, Plan, plan_for_input, request_max_tokens
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
    reasoning (distinct from the main trace it was re-fed).

    `finish_reason` / `stop` are the followup stream's, never the main's.
    A network / input-too-long failure emits `finish_reason="error"` so
    `summarize_followup_cut` (which keys on `"length"`) stays honest.

    A CUT followup (`finish_reason == "length"` or `stop == "think_cap"`)
    is never returned whole — a half-written body spliced as a finished one
    is the same class as treating a length-hit main stream as complete. It
    is not discarded whole either: `result` then holds
    :func:`salvage_complete_blocks` of the raw reply, the prefix ending on
    the last closed `</FN>` / `</FILE>`, and `salvaged_chars` records how
    much of `raw_chars` survived. The followup writes the reply for 1 595 of
    the 1 596 followups on record, so discarding a cut one discards the
    fill; keeping its closed prefix loses only the element the cut damaged.
    `salvaged_chars == 0` with a nonempty `raw_chars` is a reply that closed
    nothing — no usable answer, and `result` is `""`."""

    result: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    stop: str | None = None
    raw_chars: int = 0
    salvaged_chars: int = 0

    @property
    def cut(self) -> bool:
        """The followup stream stopped on a budget, not on an answer."""
        return self.finish_reason == "length" or self.stop == "think_cap"

    @property
    def complete(self) -> bool:
        """True iff the followup finished a body of its own accord."""
        return bool(self.result) and not self.cut

    @property
    def spliceable(self) -> bool:
        """True iff `result` is structurally whole and may replace the main
        reply — a clean finish, or the closed prefix salvaged from a cut."""
        return bool(self.result)


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
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._run_messages(
            transport, messages, digest_text=prompt, model=model,
            max_tokens=max_tokens, want_think=want_think,
            temperature=temperature, followup_think=followup_think)

    def run_turns(
        self,
        transport: Transport,
        *,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        want_think: bool | None = None,
        temperature: float | None = None,
        followup_think: bool = False,
    ) -> str:
        """One model call over a caller-built TRANSCRIPT — system, then the
        alternating user/assistant turns of a session — with the same budget,
        capping, followup and provenance policy as :meth:`run`.

        Why a second entry point rather than a prompt: the stateless fill
        route rebuilt an 88K-char document for every attempt and the model
        kept nothing of its own previous answer or the result it earned.
        Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): six attempts on
        one two-fn write, four of them the same private-field diagnostic,
        the fourth arriving one iteration after the third because the
        accessor list a retry had carried lived in a per-call slot. A
        transcript carries the model's own prior turns and the harness's
        results as turns; chat templates drop earlier assistant turns'
        reasoning blocks (measured 2026-08-09), so what persists is
        answers and results, never raw reasoning. `messages` is copied;
        the caller's list is not mutated by the followup path."""
        turns = [dict(m) for m in messages]
        return self._run_messages(
            transport, turns, digest_text=turns_digest_text(turns),
            model=model, max_tokens=max_tokens, want_think=want_think,
            temperature=temperature, followup_think=followup_think)

    def _run_messages(
        self,
        transport: Transport,
        messages: list[dict[str, Any]],
        *,
        digest_text: str,
        model: str,
        max_tokens: int,
        want_think: bool | None,
        temperature: float | None,
        followup_think: bool,
    ) -> str:
        spec = transport.spec()
        state = transport.budget_state()
        msg_chars = sum(len(m.get("content") or "") for m in messages)

        enable_thinking = transport.resolve_thinking(model, want_think)
        # The server's own count of this request when it has one: the
        # chars-per-token average is shared by prompts of every size, so a
        # few short ones move it and the next long prompt is under-counted
        # into a 400 whose "at least N" is only the threshold it crossed.
        counted = transport.count_input_tokens(
            messages, enable_thinking=enable_thinking)
        plan = request_max_tokens(
            spec, state,
            input_chars=msg_chars,
            caller_max_tokens=max_tokens,
            default_sentinel=self._sentinel,
            enable_thinking=enable_thinking,
            input_tokens=counted,
        )
        # Announce the adaptive token budget so the monitor's budget bar is
        # live during this call (not just after a cpt update lands).
        self._h.emit("vllm.budget", {"request_max": plan.request_max})

        # `cap` alone needs a pre-loop value: it is assigned only when
        # `_consume` RETURNS, so a loop exit that is not that break would read
        # it unbound. Binding an empty `_Captured()` is what lets the read
        # below drop `assert cap is not None` — an assert is removed under
        # `python -O`, i.e. absent from exactly the runs this must hold in.
        # `meta` needs no such bind: it is assigned at the top of every
        # iteration, before the stream runs.
        cap = _Captured()
        for attempt in range(_MAX_ATTEMPTS):
            self._h.sock_open(f"{transport.served_model} think={enable_thinking} attempt={attempt}")
            meta = StreamMeta()
            try:
                cap = self._consume(
                    transport.stream(messages, model=model, max_tokens=plan.request_max,
                                     enable_thinking=enable_thinking, meta=meta,
                                     temperature=temperature),
                    plan.thinking_char_cap, model,
                )
                break
            except TransportTimeout as exc:
                self._h.dbg(f"CHUNK_TIMEOUT model={model} | {exc}")
                self._h.sock_write(f"\n[TIMEOUT: {exc}]\n")
                self._h.sock_close()
                return ""
            except InputTooLong as exc:
                self._h.sock_close()
                # The 400's "at least N" is ctx - max_tokens + 1, a bound; the
                # exact count (asked again, the first ask may have failed)
                # is the measurement when the server gives one.
                if counted is None:
                    counted = transport.count_input_tokens(
                        messages, enable_thinking=enable_thinking)
                real = max(exc.real_tokens or 0, counted or 0) or None
                if attempt >= _MAX_ATTEMPTS - 1 or real is None:
                    raise
                prev, new = state.update(msg_chars, real)
                plan = self._replan_with_input(spec, state, real, max_tokens,
                                               enable_thinking, plan)
                self._h.guard("vllm/400_retry",
                              f"attempt={attempt} reported_bound={exc.real_tokens}tok "
                              f"counted={counted}tok "
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
        prompt_sha = PromptDigest.of(digest_text).sha256
        fu = _FollowupOutcome()
        if followup:
            fu = self._followup(transport, messages, reasoning_text, model, max_tokens,
                                prompt_sha=prompt_sha, followup_think=followup_think)
            # A failed followup, or one that closed no element, must not
            # replace the main result — `fu.result or result` used to splice
            # a half-written body as a finished one. A CUT followup still
            # contributes its closed prefix (`salvage_complete_blocks`),
            # because this completion writes the reply for 1 595 of the
            # 1 596 followups on record and dropping it whole drops the fill.
            if fu.spliceable:
                result = fu.result

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
            "think_char_cap": plan.thinking_char_cap,
            "enable_thinking": enable_thinking,
            # So a reader replaying events.jsonl (the monitor's live cut-rate
            # gauge) can exclude bookkeeping calls the same way
            # `record_stream_meta` excludes them from the in-process
            # run-level fold below — by the fact itself, not a caller-site
            # guess reconstructed after the fact.
            "bookkeeping": is_bookkeeping_call(),
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
            "think_char_cap": plan.thinking_char_cap,
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
        71 584 reasoning chars, and the reply on record was this followup's
        (`[llm/followup] thinking_len=71584 result_len=8192`); record-wide,
        1 595 of 1 596 followup replies. That 8 192 is CHARACTERS and the
        followup's budget was 8 192 TOKENS — the reply carried 21 `<FN ` and
        21 `</FN>` and ended on a closer, so it finished of its own accord
        inside about a third of its budget. It was not starved. With True the
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
        fu_counted = transport.count_input_tokens(
            fu_messages, enable_thinking=followup_think)
        fu_plan = request_max_tokens(
            spec, state, input_chars=fu_chars, caller_max_tokens=max_tokens,
            default_sentinel=self._sentinel, enable_thinking=followup_think,
            input_tokens=fu_counted,
        )
        self._h.dbg(f"FOLLOWUP model={model} thinking_len={len(reasoning_text)} "
                    f"fu_max={fu_plan.request_max} think={followup_think}")
        self._h.sock_open(f"{transport.served_model}-followup think={followup_think}")
        self._h.sock_write("\n[FOLLOWUP — thinking budget exhausted, requesting code]\n")
        # See `run`: `cap` is assigned only on `_consume`'s return, so the
        # pre-loop bind is what makes the post-loop read total without an
        # `assert` that `python -O` would remove.
        cap = _Captured()
        for attempt in range(_MAX_ATTEMPTS):
            meta = StreamMeta()
            try:
                cap = self._consume(
                    transport.stream(fu_messages, model=model, max_tokens=fu_plan.request_max,
                                     enable_thinking=followup_think, meta=meta),
                    fu_plan.thinking_char_cap if followup_think else None, model,
                )
                break
            except (TransportTimeout, TransportUnavailable) as exc:
                self._h.guard("llm/followup_network", f"err={type(exc).__name__}: {exc}")
                self._emit_followup_meta(
                    prompt_sha=prompt_sha, model=model, finish_reason="error",
                    result="", fu_reasoning="", reasoning_text=reasoning_text,
                    messages=messages, fu_messages=fu_messages,
                    request_max=fu_plan.request_max, followup_think=followup_think,
                    stop=None)
                return _FollowupOutcome(finish_reason="error")
            except InputTooLong as exc:
                # The main stream retries this (run() :181); the followup
                # used to let it escape unemitted, so a followup 400 left
                # no `llm.followup_meta` row at all.
                if fu_counted is None:
                    fu_counted = transport.count_input_tokens(
                        fu_messages, enable_thinking=followup_think)
                fu_real = max(exc.real_tokens or 0, fu_counted or 0) or None
                if attempt >= _MAX_ATTEMPTS - 1 or fu_real is None:
                    self._h.guard("llm/followup_input",
                                  f"err=InputTooLong real_tokens={fu_real}")
                    self._emit_followup_meta(
                        prompt_sha=prompt_sha, model=model, finish_reason="error",
                        result="", fu_reasoning="", reasoning_text=reasoning_text,
                        messages=messages, fu_messages=fu_messages,
                        request_max=fu_plan.request_max, followup_think=followup_think,
                        stop=None)
                    return _FollowupOutcome(finish_reason="error")
                state.update(fu_chars, fu_real)
                fu_plan = self._replan_with_input(
                    spec, state, fu_real, max_tokens, followup_think, fu_plan)
                self._h.guard("llm/followup_input_retry",
                              f"attempt={attempt} real_input={fu_real}tok "
                              f"new_req_max={fu_plan.request_max}")
                continue
        raw = transport.clean("".join(cap.content), model=model)
        fu_reasoning = "".join(cap.reasoning)
        # A cut reply keeps only what it closed. `salvage_complete_blocks`
        # returns the whole string when the cut landed on a closer, and `""`
        # when the reply closed nothing — a mid-body truncation of a single
        # element carries no usable answer.
        was_cut = meta.finish_reason == "length" or cap.stop == "think_cap"
        usable = salvage_complete_blocks(raw) if was_cut else raw
        if was_cut and raw:
            self._h.guard(
                "llm/followup_cut",
                f"model={model} finish={meta.finish_reason} stop={cap.stop} "
                f"raw_chars={len(raw)} salvaged_chars={len(usable)} "
                f"fn_blocks={usable.count('<FN ')}")
        self._h.guard("llm/followup", f"model={model} thinking_len={len(reasoning_text)} result_len={len(usable)}")
        self._emit_followup_meta(
            prompt_sha=prompt_sha, model=model, finish_reason=meta.finish_reason,
            result=raw, fu_reasoning=fu_reasoning, reasoning_text=reasoning_text,
            messages=messages, fu_messages=fu_messages,
            request_max=fu_plan.request_max, followup_think=followup_think,
            stop=cap.stop, salvaged_chars=len(usable) if was_cut else 0)
        return _FollowupOutcome(
            result=usable, reasoning=fu_reasoning,
            finish_reason=meta.finish_reason, stop=cap.stop,
            raw_chars=len(raw), salvaged_chars=len(usable) if was_cut else 0)

    def _emit_followup_meta(
        self, *, prompt_sha: str, model: str, finish_reason: str | None,
        result: str, fu_reasoning: str, reasoning_text: str,
        messages: list[dict[str, Any]], fu_messages: list[dict[str, Any]],
        request_max: int | None, followup_think: bool, stop: str | None,
        salvaged_chars: int = 0,
    ) -> None:
        """`result` is the RAW reply, so `content_chars` measures what the
        stream produced; `salvaged_chars` is how much of it survived the cut
        (0 when the reply was not cut). A reader can therefore tell a clean
        finish from a cut that kept everything from a cut that kept nothing,
        which `content_chars` alone never could."""
        fu_meta = {
            "prompt_sha": prompt_sha,
            "model": model,
            "finish_reason": finish_reason,
            "content_chars": len(result),
            "salvaged_chars": salvaged_chars,
            "reasoning_chars": len(fu_reasoning),
            "reasoning_chars_refed": refed_reasoning_chars(
                reasoning_text, messages, fu_messages),
            "request_max": request_max,
            "enable_thinking": followup_think,
            "stop": stop,
            "fn_blocks": result.count("<FN "),
        }
        self._h.emit("llm.followup_meta", fu_meta)
        record_followup_meta(fu_meta)

    @staticmethod
    def _replan_with_input(spec: BudgetSpec, state: BudgetState, real_tokens: int,
                           max_tokens: int, enable_thinking: bool, prev_plan: Plan) -> Plan:
        """Recompute the budget plan from the server's reported input-token
        count. The server reports "at least N" — a LOWER bound, not exact;
        treating it as exact left the retry ~1 token over ctx on every
        attempt (anyhow 2026-07-02: 25365+40172 = 65537 > 65536).
        Pad it before capping so the retry actually fits. The plan itself
        comes from `plan_for_input`, the same function the first request
        used, so a per-call plan stays per-call on the retry."""
        padded_input = int(real_tokens * 1.02) + 256
        plan = plan_for_input(spec, input_tokens=padded_input,
                              content_cap=prev_plan.content_cap,
                              enable_thinking=enable_thinking)
        return replace(plan, est_input_tokens=real_tokens)
