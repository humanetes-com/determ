"""LLM transports (adapters) — the dumb token pipes behind the LLM port.

Relocated here from model_interact.py, which stays as a thin shim. Each transport
opens a connection, streams normalized `Delta` events, and declares a `BudgetSpec`;
ALL recovery/capping/followup/retry policy lives in `pipeline_core.llm`'s
`LLMController`. A transport must never reintroduce a private max_tokens cap or
its own followup. `model_interact.py` is now a thin shim re-exporting these +
the composition root (ask/CLI).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

import httpx

from pipeline_core.llm import (
    BudgetSpec,
    BudgetState,
    Delta,
    Hooks,
    InputTooLong,
    StreamMeta,
    Transport,
    TransportTimeout,
    TransportUnavailable,
)
from pipeline_core.llm.cleanup import strip_chat_boundaries, strip_think
from pipeline_core.llm.thinktag import InlineThinkSplitter
from pipeline_core.log import emit as _emit_event  # structured event sink

MODEL = os.getenv("MODEL", "qwen2.5-coder:7b")

def _default_max_tokens() -> int:
    _mt = os.getenv("MAX_TOKENS")
    if _mt:
        return int(_mt)
    # Derive from context window — leave 8K tokens minimum for the prompt.
    num_ctx = os.getenv("OLLAMA_NUM_CTX")
    if num_ctx and num_ctx.strip().isdigit():
        return max(int(num_ctx) - 8192, 8192)
    vllm_ctx = os.getenv("VLLM_MAX_MODEL_LEN")
    if vllm_ctx and vllm_ctx.strip().isdigit():
        return max(int(vllm_ctx) - 8192, 8192)
    return 32768  # safe fallback for unknown backends

MAX_TOKENS = _default_max_tokens()

# Per-model capability map.
#
# think        — whether the model should use extended reasoning (think:true/false).
#                Qwen3.6 is a hybrid-thinking model: think=True is recommended for
#                complex coding tasks (better quality, "preserve thinking" across turns).
#                Use think=False only for simple/fast tasks.
#
# native_ollama — whether to use the ollama Python library (/api/chat) instead of the
#                OpenAI-compat endpoint (/v1/chat/completions).
#                Qwen3.6+ requires native endpoint: the compat layer routes thinking
#                tokens into reasoning_content (not content), so our streaming loop
#                receives an empty string when think=True via compat.
#                qwen3:32b works fine via compat (older Qwen3 family, no this issue).
#
# reasoning     — WHICH chat-template variable turns extended reasoning on and off
#                for this model. Two dialects exist among the models served here and
#                they are not interchangeable:
#
#                "enable_thinking"  — Qwen3.6's template branches on a boolean
#                    `enable_thinking`, so False actually suppresses the think block.
#
#                "reasoning_strength" — Muse-Glimmer-30B's template has NO thinking
#                    boolean. It reads a level and renders it into the system prompt:
#                        {%- set rs = reasoning_strength if reasoning_strength is
#                           defined and reasoning_strength else 'high' -%}
#                        {{- 'Reasoning strength: ' + rs + '.' -}}
#                    (chat_template.jinja, meta-models/Muse-Glimmer-30B, read
#                    2026-08-10). Levels are low / medium / high / xhigh; the card
#                    recommends high or xhigh for coding and agentic work, and the
#                    template's own default when the variable is absent is 'high'.
#
#                The failure this key exists to prevent: an unknown template
#                variable is not an error, it is silently dropped. Sending
#                `enable_thinking: false` to Muse Glimmer leaves `rs` undefined,
#                the template falls back to 'high', and the request that asked for
#                NO reasoning gets the most. The followup path depends on that
#                suppression working — it re-feeds the reasoning and asks for code
#                only — so on the wrong dialect it would re-reason instead.
#
# History:
#   qwen3:32b        — confirmed good results via OpenAI-compat + think:true (sessions 3-9)
#   qwen3.6:27b      — needs native ollama endpoint; think:true recommended for coding tasks
#   qwen3.6-27b-q8   — SGLang served name for the same model; uses OpenAI-compat + thinking_budget
REASONING_ENABLE_THINKING = "enable_thinking"
REASONING_STRENGTH = "reasoning_strength"

MODEL_CAPS: dict[str, dict[str, Any]] = {
    "qwen3:32b":                  {"think": True,  "native_ollama": False},
    "qwen3.6:27b":                {"think": True,  "native_ollama": True},
    # `think: False` from its introduction (2026-05-16) until 2026-08-09, and
    # never once True, while the stated intent for this model was always
    # think=True — code and intent had never agreed. The pin was a workaround
    # for `_stream_compat` reading only `delta.content`: Qwen3.6 puts reasoning
    # in a separate field, so content stayed empty for the whole reasoning phase
    # and the response came back blank. That path now reads both field names,
    # so the pin has nothing left to work around.
    "qwen3.6:27b-q8":             {"think": True,  "native_ollama": False},
    "qwen3.6-27b-q8":             {"think": True,  "native_ollama": False},   # SGLang served name (dash, not colon)
    "Qwen/Qwen3.6-27B-FP8":                      {"think": True,  "native_ollama": False},
    "mattbucci/Qwen3.6-27B-AWQ":                  {"think": True,  "native_ollama": False},
    "/home/user/Qwen3.6-27B-Q8_0.gguf":          {"think": True,  "native_ollama": False},
    "meta-models/Muse-Glimmer-30B":               {"think": True,  "native_ollama": False,
                                                  "reasoning": REASONING_STRENGTH},
    "RedHatAI/Muse-Glimmer-30B-FP8-block":        {"think": True,  "native_ollama": False,
                                                  "reasoning": REASONING_STRENGTH},
}

# The two rungs the boolean `enable_thinking` maps onto in the strength dialect.
# There is no "off": the levels are low / medium / high / xhigh, so a request
# that wants no reasoning asks for the least. Both are overridable because the
# right ON rung is a quality/latency trade the card leaves open (it recommends
# "high or xhigh" for coding and agentic tasks) and only a measured run here can
# settle it.
_REASONING_STRENGTH_ON = os.getenv("VLLM_REASONING_STRENGTH", "high")
_REASONING_STRENGTH_OFF = os.getenv("VLLM_REASONING_STRENGTH_OFF", "low")


def _model_uses_think(model: str) -> bool:
    return bool(MODEL_CAPS.get(model, {}).get("think", True))


def _model_use_native_ollama(model: str) -> bool:
    return bool(MODEL_CAPS.get(model, {}).get("native_ollama", False))


def _model_reasoning_dialect(model: str) -> str:
    """Which chat-template variable expresses reasoning on/off for this model.

    Defaults to the boolean dialect: it is what every model served here before
    Muse Glimmer used, and a model whose template has neither variable drops it
    either way."""
    return str(MODEL_CAPS.get(model, {}).get("reasoning", REASONING_ENABLE_THINKING))


def reasoning_template_kwargs(model: str, enable_thinking: bool) -> dict[str, Any]:
    """The `chat_template_kwargs` payload that asks `model` for (no) reasoning."""
    if _model_reasoning_dialect(model) == REASONING_STRENGTH:
        return {REASONING_STRENGTH: _REASONING_STRENGTH_ON if enable_thinking
                else _REASONING_STRENGTH_OFF}
    return {REASONING_ENABLE_THINKING: enable_thinking}


# ---------------------------------------------------------------------------
# Backends are TRANSPORTS only: open a connection, stream normalized Delta events,
# declare a BudgetSpec. All recovery / capping / followup / retry policy lives once
# in pipeline_core.llm.LLMController (see that package's docstring). A transport must
# never reintroduce a private max_tokens cap or its own followup.
# ---------------------------------------------------------------------------

# Big context default for transports that don't cap by ctx (SGLang/HF).
_UNBOUNDED_CTX = 131072


class _BaseTransport:
    """Convenience defaults for the simple, non-thinking, non-followup transports."""

    served_model: str = MODEL
    supports_followup: bool = False

    def __init__(self) -> None:
        self._state = BudgetState()

    def budget_state(self) -> BudgetState:
        return self._state

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        return False

    def count_input_tokens(self, messages: list[dict[str, Any]], *,
                           enable_thinking: bool) -> int | None:
        return None

    def followup_messages(self, base_messages: list[dict[str, Any]], reasoning_text: str,
                          *, model: str) -> list[dict[str, Any]]:
        return base_messages

    def clean(self, text: str, *, model: str) -> str:
        return strip_think(text)

    @staticmethod
    def _refeed_as_user_turn(base_messages: list[dict[str, Any]],
                             reasoning_text: str) -> list[dict[str, Any]]:
        """Re-feed reasoning in a USER turn and ask for code only.

        NOT an assistant turn carrying `<think>…</think>`, which is what every
        OpenAI-compatible path here used to send: Qwen3-family chat templates
        DISCARD think blocks in prior assistant turns, so the followup re-fed
        nothing while telling the model it had "already reasoned above".
        Measured against the live server on 2026-08-09 — prompt_tokens for that
        assistant turn against reasoning of 0 / 2,000 / 20,000 / 121,481 chars
        was 33 / 33 / 33 / 33, versus 33 / 1,034 / 10,034 / 60,774 for the same
        bodies in a user turn.

        The native Ollama `/api/chat` path is exempt and keeps its assistant
        turn: it sets the `thinking` FIELD, which the endpoint reads directly
        rather than parsing out of content.
        """
        return base_messages + [
            {"role": "user", "content": (
                "Your earlier reasoning on this exact task:\n\n"
                f"{reasoning_text}\n\n"
                "Now write the code block. Do not re-explain — output only the code."
            )},
        ]

    def wait_until_ready(self) -> None:
        return None


# Repo-root logs/ (this module lives at <root>/pipeline_core/adapters/; the logs
# dir is two levels up — NOT next to this file — so streams/debug/guard logs land
# where the monitor and the rest of the pipeline read them).
_LOG_DIR          = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
_DEBUG_LOG        = os.getenv("LLM_DEBUG_LOG", os.getenv("OLLAMA_DEBUG_LOG", os.path.join(_LOG_DIR, "debug.log")))
_STREAM_LOG       = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", os.path.join(_LOG_DIR, "stream.log")))
# Silence timeout: if no token arrives for this many seconds, treat as stalled.
_CHUNK_TIMEOUT    = int(os.getenv("OLLAMA_CHUNK_TIMEOUT", "60"))
# Thinking cap: break out of thinking loop after this many chars (~10k tokens).
# Falls through to the existing followup "now write the code" path.
_MAX_THINK_CHARS  = int(os.getenv("OLLAMA_MAX_THINK_CHARS", "40000"))

# The same cap for vLLM, which ran UNCAPPED until 2026-08-09 and so had nothing
# to interrupt a runaway reasoning stream. Four calls in run `491052-1af6b4f4`
# each consumed the entire 32,768-token budget inside the reasoning block and
# produced ZERO characters of answer, at 856, 872, 1,364 and 856 seconds —
# 65.8 minutes of a 464-minute run:
#
#   [18:07:30] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,481)
#   [20:36:53] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,109)
#   [22:03:22] llm/length_truncated hit max_tokens=32768 (content=0, thinking=121,432)
#   [22:44:43] llm/length_truncated hit max_tokens=32768 (content=0, thinking=124,043)
#
# The exact value is immaterial and that is measured, not assumed: the cap sits
# in a measured EMPTY BAND between the healthy population and the runaways, so
# any value inside the band separates the two. The band has moved once as the
# served model changed. Qwen era: across 226 recorded traces the distribution
# was sharply bimodal — 0–36,570 chars against 69,830–152,088, healthy fill-probe
# reasoning at 7,000–12,000. Muse moved the low edge: measured uncapped on
# retained anyhow prompts (scripts/measure_muse_reasoning.sh, 2026-08-13,
# logs/reasoning_econ/calls.jsonl), Muse@high finishes naturally at med 46,562 /
# p90 73,718 / max 77,595 chars — healthy calls populate the old band's lower
# half, and a 60,000 cap was breaking p90-healthy streams (14.4% of calls on run
# `298540-f792975a`). The runaways still start at 121,109 (the smallest of the
# truncated traces above), so the band is now 77,595–121,109.
#
# This is NOT the May 2026 `safe_max_tokens` bug rebuilt. That one predicted a
# token count from `chars // 4`, was off by 8,449 tokens against Qwen3's denser
# tokenizer, and returned HTTP 400 on `input + max_tokens > ctx`. This predicts
# nothing: `_consume` sums `len(delta.reasoning)` over deltas ALREADY RECEIVED
# and breaks on the running total. No tokenizer, no context arithmetic, and the
# error is one-sided — breaking early strictly reduces tokens used, so it cannot
# overflow. A wrong value costs answer quality, visible as `llm/think_cap`.
# The default is the measured value, NOT `_MAX_THINK_CHARS`. Falling back to the
# Ollama cap put 40,000 — mid-healthy-population — into every environment that
# does not set `VLLM_MAX_THINK_CHARS`, and `.env` is untracked, so that was every
# fresh clone and the CI runner. It went unseen because this box's `.env` carries
# `VLLM_MAX_THINK_CHARS=120000` (set 2026-08-13 from the measurement above), which
# is also what made `tests/test_think_cap.py` green here and red anywhere else:
# the band assertion was reading the operator's config, not the shipped default.
# 120,000 sits above every healthy call observed in both measured cells (max
# 77,595) and below the smallest recorded runaway (121,109), consistent with the
# 32,768-token request ceiling. `.env` still overrides.
#
# The band above is REFUTED (2026-09-21) and so is the premise that a fixed
# character value can separate healthy calls from runaways. Its high edge was
# never a runaway population: the four traces are 121,109–124,043 chars at
# `max_tokens=32768`, i.e. 3.70–3.79 chars per token — every one of them is the
# REQUEST CEILING, a call that ran out of tokens, not one that would not stop.
# Re-asked with the room the context allows (scripts/measure_capped_uncapped.py
# over the cut file-route prompts of runs 450977-5f48b68a, 195091-72b92f1e and
# 3400-4d0618ba), calls that had been cut at 120,000 finished on their own at
# 93,740–159,457 chars with a body; on the live route the healthy distribution
# reached p50 73,962 / max 119,973 over 383 calls, 14% cut. There is no empty
# band to sit in. `VLLM_REASONING_BUDGET=per_call` (`llm/budget.py`) removes the
# character cap and gives each call the context its prompt leaves; this value
# is only the `fixed` shape's cap.
_VLLM_MAX_THINK_CHARS = int(os.getenv("VLLM_MAX_THINK_CHARS", "120000"))

# The recorded evidence both claims rest on, kept next to the value so the next
# change is checked against it rather than against a remembered number.
# Truncations at the 32,768-token ceiling (run 491052-1af6b4f4, 2026-08-08):
VLLM_CEILING_TRUNCATIONS_CHARS: tuple[int, ...] = (121_481, 121_109, 121_432, 124_043)
VLLM_CEILING_TRUNCATIONS_MAX_TOKENS = 32_768
# Natural finishes of prompts that the 120,000 cap had cut (2026-09-21):
VLLM_UNCAPPED_NATURAL_FINISHES_CHARS: tuple[int, ...] = (
    155_176, 96_368, 93_740, 127_950, 101_209, 140_765, 159_457)


def reasoning_budget_is_per_call(value: str) -> bool:
    """`VLLM_REASONING_BUDGET` → True for `per_call`, False for `fixed`.

    Anything else raises: a typo silently falling back to `fixed` would put the
    run back under the character cap the operator meant to remove, and the
    only symptom is the cut rate the knob was set to end."""
    v = value.strip().lower()
    if v in ("per_call", "per-call"):
        return True
    if v in ("fixed", ""):
        return False
    raise ValueError(
        f"VLLM_REASONING_BUDGET={value!r}: expected 'fixed' or 'per_call'")



_GUARD_LOG = os.getenv("PIPELINE_GUARD_LOG", os.path.join(_LOG_DIR, "pipeline_guards.log"))


def _dbg(msg: str) -> None:
    if not _DEBUG_LOG:
        return
    import datetime
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with open(_DEBUG_LOG, "a") as fh:
        fh.write(line)


def _guard(location: str, msg: str) -> None:
    """Log a response-side truncation event to both debug log and guard log file."""
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"  GUARD: [{location}] {msg}"
    _dbg(f"GUARD [{location}] {msg}")
    try:
        with open(_GUARD_LOG, "a") as fh:
            fh.write(f"[{ts}] {line}\n")
    except Exception:
        pass


def _dbg_response(label: str, raw: str) -> None:
    if not _DEBUG_LOG:
        return
    has_open  = "<think>" in raw
    has_close = "</think>" in raw
    snippet   = (raw[:200] + " … " + raw[-100:]) if len(raw) > 300 else raw
    snippet   = snippet.replace("\n", "↵")
    _dbg(f"{label} | len={len(raw)} | <think>={has_open} </think>={has_close} | {snippet!r}")


# Per-call stream file handle. Overwritten at the start of each call so any
# number of readers (monitor, logger, etc.) can open and tail the file
# simultaneously without socket-ownership conflicts.
# Per-thread stream handle. PIPELINE_PARALLELISM>1 runs N concurrent ask()
# workers; one shared handle would interleave their token streams and
# re-truncate the file on every worker's _sock_open. Each thread owns its own
# file so the live logs stay coherent.
_stream_tl = threading.local()


def _stream_path_for_thread() -> str:
    """Main thread → the base stream log (so `tail -f stream.log` still works);
    worker threads → a sibling file keyed by thread name."""
    if not _STREAM_LOG:
        return ""
    t = threading.current_thread()
    if t is threading.main_thread():
        return _STREAM_LOG
    safe = t.name.replace("/", "_").replace(" ", "_")
    root, dot, ext = _STREAM_LOG.rpartition(".")
    return f"{root}.{safe}.{ext}" if dot else f"{_STREAM_LOG}.{safe}"


def _sock_open(label: str = "") -> None:
    """Open this thread's stream log for this call (overwrite). Skip on error."""
    import time as _time
    fh = getattr(_stream_tl, "fh", None)
    if fh:
        with contextlib.suppress(Exception):
            fh.close()
    _stream_tl.fh = None
    path = _stream_path_for_thread()
    if not path:
        return
    try:
        fh = open(path, "w", buffering=1)  # noqa: SIM115 — long-lived per-thread stream-log handle; closed on next _sock_open / _sock_close
        fh.write(f"[CALL_START: {_time.time():.3f}]\n")
        if label:
            fh.write(f"[CALL: {label}]\n")
        fh.flush()
        _stream_tl.fh = fh
    except Exception:
        _stream_tl.fh = None


def _sock_write(text: str) -> None:
    fh = getattr(_stream_tl, "fh", None)
    if not fh or not text:
        return
    try:
        fh.write(text)
        fh.flush()
    except Exception:
        pass


def _sock_close() -> None:
    fh = getattr(_stream_tl, "fh", None)
    if fh:
        with contextlib.suppress(Exception):
            fh.close()
    _stream_tl.fh = None


def _timed_iter(it: Iterable[Any], timeout: int) -> Iterator[Any]:
    """Wrap an iterator; raise TimeoutError if no item arrives within `timeout` seconds."""
    import queue as _q
    import threading as _t
    q: _q.Queue[object] = _q.Queue()
    sentinel = object()

    def _feed() -> None:
        try:
            for item in it:
                q.put(item)
        except Exception as exc:
            q.put(exc)
        finally:
            q.put(sentinel)

    _t.Thread(target=_feed, daemon=True).start()
    while True:
        try:
            item = q.get(timeout=timeout)
        except _q.Empty:
            raise TimeoutError(f"no token received in {timeout}s — generation stalled") from None
        if item is sentinel:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


class OllamaTransport(_BaseTransport):
    """Ollama via native /api/chat (Qwen3.6+) or the OpenAI-compat /v1 endpoint.

    Path is chosen per-call by the model's capability map. Both are normalized to the
    same Delta(content, reasoning) stream so the controller is unaware of the split.
    """

    supports_followup = True

    def __init__(self) -> None:
        super().__init__()
        self._base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self._num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        self.served_model = MODEL

    def spec(self) -> BudgetSpec:
        return BudgetSpec(
            ctx_len=self._num_ctx, content_budget=0, thinking_budget=0, margin=256,
            cpt_floor=3.5, adaptive_cpt=False, add_thinking_to_request=False,
            cap_by_ctx=True, send_max_tokens=True, thinking_char_cap=_MAX_THINK_CHARS,
        )

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        return _model_uses_think(model) if want_think is None else want_think

    def stream(self, messages: list[dict[str, Any]], *, model: str,
               max_tokens: int | None, enable_thinking: bool,
               meta: StreamMeta, temperature: float | None = None) -> Iterator[Delta]:
        if _model_use_native_ollama(model):
            yield from self._stream_native(messages, model, enable_thinking, meta, temperature)
        else:
            yield from self._stream_compat(messages, model, max_tokens, enable_thinking,
                                           meta, temperature)

    def _stream_native(self, messages: list[dict[str, Any]], model: str,
                       enable_thinking: bool, meta: StreamMeta,
                       temperature: float | None = None) -> Iterator[Delta]:
        import ollama as _ollama
        options: dict[str, Any] = {"num_ctx": self._num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        try:
            for part in _timed_iter(
                _ollama.chat(model=model, messages=messages, think=enable_thinking,
                             stream=True, options=options),
                _CHUNK_TIMEOUT,
            ):
                # The final chunk (`done`) carries the server's own counts.
                if getattr(part, "done", False):
                    _read_ollama_usage(part, meta)
                thinking = getattr(part.message, "thinking", None)
                if thinking:
                    yield Delta(reasoning=thinking)
                delta = part.message.content
                if delta:
                    yield Delta(content=delta)
        except TimeoutError as exc:
            raise TransportTimeout(str(exc)) from exc

    def _stream_compat(self, messages: list[dict[str, Any]], model: str,
                       max_tokens: int | None, enable_thinking: bool,
                       meta: StreamMeta, temperature: float | None = None) -> Iterator[Delta]:
        from openai import APIConnectionError, OpenAI
        client = OpenAI(base_url=self._base_url, api_key="ollama", timeout=300)
        # Qwen3 compat layer has no think param — /no_think suppresses reasoning.
        if not enable_thinking and messages and messages[-1]["role"] == "user":
            messages = messages[:-1] + [
                {**messages[-1], "content": "/no_think " + messages[-1]["content"]}
            ]
        splitter = InlineThinkSplitter()
        # Route connection/timeout failures (httpx + the OpenAI SDK's own
        # APIConnectionError wrapper) through TransportUnavailable so the
        # controller's wait_until_ready() readiness poll absorbs a server
        # restart/warmup instead of aborting the run.
        try:
            with client.chat.completions.create(  # type: ignore[call-overload]
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                extra_body={"options": {"num_ctx": self._num_ctx}},
                stream=True,
                stream_options={"include_usage": True},
                **({"temperature": temperature} if temperature is not None else {}),
            ) as stream:
                for chunk in stream:
                    _read_openai_usage(chunk, meta)
                    if not chunk.choices:
                        continue
                    _d = chunk.choices[0].delta
                    # Two reasoning conventions, and reading only the first is
                    # what pinned `qwen3.6:27b-q8` to `think: False`. Qwen3 and
                    # older emit reasoning INLINE in `content` wrapped in
                    # `<think>` tags, which `InlineThinkSplitter` handles.
                    # Qwen3.6 emits it in a SEPARATE field instead, so on this
                    # path `content` stayed empty through the whole reasoning
                    # phase and the splitter found no tags to split — an empty
                    # response, hence the pin. `VLLMTransport.stream` has read
                    # both field names since it was written; the dual read was
                    # never backported here.
                    _reasoning = (getattr(_d, "reasoning", None)
                                  or getattr(_d, "reasoning_content", None))
                    if _reasoning:
                        yield Delta(reasoning=_reasoning)
                    delta = _d.content
                    if not delta:
                        continue
                    c, r = splitter.feed(delta)
                    if r:
                        yield Delta(reasoning=r)
                    if c:
                        yield Delta(content=c)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError,
                APIConnectionError) as exc:
            raise TransportUnavailable(str(exc)) from exc
        c, r = splitter.finish()
        if r:
            yield Delta(reasoning=r)
        if c:
            yield Delta(content=c)

    def followup_messages(self, base_messages: list[dict[str, Any]], reasoning_text: str,
                          *, model: str) -> list[dict[str, Any]]:
        if _model_use_native_ollama(model):
            # Native endpoint accepts the assistant `thinking` field directly.
            return base_messages + [
                {"role": "assistant", "thinking": reasoning_text, "content": ""},
                {"role": "user", "content": (
                    "You have already reasoned through this problem above. "
                    "Now write the code block. Do not re-explain — output only the code."
                )},
            ]
        # OpenAI-compat path: the template strips an assistant think block, so
        # the reasoning has to travel as a user turn. See `_refeed_as_user_turn`.
        tail = reasoning_text[-6000:] if len(reasoning_text) > 6000 else reasoning_text
        return self._refeed_as_user_turn(base_messages, tail)

    def clean(self, text: str, *, model: str) -> str:
        result, events = strip_chat_boundaries(text, model=model)
        for loc, msg in events:
            _guard(loc, msg)
        return result


class HuggingFaceTransport(_BaseTransport):
    """Local inference via HuggingFace transformers + accelerate (non-streaming).

    The `model` passed to stream() is ignored — the loaded model is fixed at init.
    Set HF_MODEL once for all tasks.

    Environment variables:
      HF_MODEL, HF_DEVICE_MAP, HF_LOAD_IN_4BIT, HF_LOAD_IN_8BIT, HF_TORCH_DTYPE, HF_MAX_NEW_TOKENS
    """

    def __init__(self) -> None:
        super().__init__()
        self._hf_model_id = os.getenv("HF_MODEL", MODEL)
        self.served_model = self._hf_model_id
        self._device_map = os.getenv("HF_DEVICE_MAP", "auto")
        self._load_in_4bit = os.getenv("HF_LOAD_IN_4BIT", "false").lower() == "true"
        self._load_in_8bit = os.getenv("HF_LOAD_IN_8BIT", "false").lower() == "true"
        self._torch_dtype_str = os.getenv("HF_TORCH_DTYPE", "bfloat16")
        self._max_new_tokens = int(os.getenv("HF_MAX_NEW_TOKENS", str(MAX_TOKENS)))
        self._model: Any = None
        self._tokenizer: Any = None

    def spec(self) -> BudgetSpec:
        return BudgetSpec(
            ctx_len=_UNBOUNDED_CTX, content_budget=0, thinking_budget=0, margin=0,
            cpt_floor=3.0, adaptive_cpt=False, add_thinking_to_request=False,
            cap_by_ctx=False, send_max_tokens=True, thinking_char_cap=None,
        )

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        dtype_map = {"bfloat16": torch.bfloat16, "float16": torch.float16, "auto": "auto"}
        torch_dtype = dtype_map.get(self._torch_dtype_str, torch.bfloat16)

        quantization_config = None
        if self._load_in_4bit or self._load_in_8bit:
            # No `type: ignore[no-untyped-call]` here: transformers is one of the
            # optional backends pyproject pins to `follow_imports = "skip"`, so this
            # is a call on Any in every environment. The ignore this line used to
            # carry was live in the local venv (which has transformers installed for
            # this transport) and unused in CI (which installs requirements-base.txt
            # only), and `warn_unused_ignores` failed the push of 2026-08-15 on it.
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=self._load_in_4bit,
                load_in_8bit=self._load_in_8bit,
            )

        print(f"[HuggingFaceTransport] Loading {self._hf_model_id} ...", file=sys.stderr)
        self._tokenizer = AutoTokenizer.from_pretrained(self._hf_model_id)
        self._model = AutoModelForCausalLM.from_pretrained(
            self._hf_model_id,
            device_map=self._device_map,
            torch_dtype=torch_dtype,
            quantization_config=quantization_config,
        )
        print(f"[HuggingFaceTransport] Model loaded on: "
              f"{ {str(d) for d in self._model.hf_device_map.values()} }", file=sys.stderr)

    def stream(self, messages: list[dict[str, Any]], *, model: str,
               max_tokens: int | None, enable_thinking: bool,
               meta: StreamMeta, temperature: float | None = None) -> Iterator[Delta]:
        self._load()
        import torch

        text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        inputs = self._tokenizer([text], return_tensors="pt").to(self._model.device)
        _mnt = max_tokens if max_tokens is not None else self._max_new_tokens
        # Greedy by default; an explicit temperature switches on sampling.
        sample_kwargs: dict[str, Any] = (
            {"do_sample": True, "temperature": temperature} if temperature is not None
            else {"do_sample": False}
        )
        with torch.no_grad():
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=min(_mnt, self._max_new_tokens),
                **sample_kwargs,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        new_ids = output_ids[0][inputs["input_ids"].shape[1]:]
        meta.prompt_tokens = int(inputs["input_ids"].shape[1])
        meta.completion_tokens = int(len(new_ids))
        out = str(self._tokenizer.decode(new_ids, skip_special_tokens=True))
        if out:
            yield Delta(content=out)


class SGLangTransport(_BaseTransport):
    """SGLang via its OpenAI-compatible /v1/chat/completions endpoint.

    Environment variables:
      SGLANG_BASE_URL, SGLANG_MODEL_NAME, SGLANG_THINKING_BUDGET
    """

    supports_followup = True

    def __init__(self) -> None:
        super().__init__()
        self._base_url = os.getenv("SGLANG_BASE_URL", "http://localhost:30000/v1")
        self._model_name = os.getenv("SGLANG_MODEL_NAME", MODEL)
        self.served_model = self._model_name
        self._thinking_budget = int(os.getenv("SGLANG_THINKING_BUDGET", "8192"))

    def spec(self) -> BudgetSpec:
        return BudgetSpec(
            ctx_len=_UNBOUNDED_CTX, content_budget=0, thinking_budget=0, margin=0,
            cpt_floor=3.0, adaptive_cpt=False, add_thinking_to_request=False,
            cap_by_ctx=False, send_max_tokens=True, thinking_char_cap=None,
        )

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        return _model_uses_think(self._model_name) if want_think is None else want_think

    def stream(self, messages: list[dict[str, Any]], *, model: str,
               max_tokens: int | None, enable_thinking: bool,
               meta: StreamMeta, temperature: float | None = None) -> Iterator[Delta]:
        from openai import APIConnectionError, OpenAI
        client = OpenAI(base_url=self._base_url, api_key="sglang", timeout=120)
        extra_body: dict[str, Any] = {}
        if enable_thinking and self._thinking_budget > 0:
            extra_body["thinking_budget"] = self._thinking_budget
        # See _stream_compat: wrap connection/timeout failures as
        # TransportUnavailable so the controller's readiness poll handles a
        # server restart/warmup rather than aborting the run.
        try:
            with client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model_name,
                messages=messages,
                max_tokens=max_tokens,
                extra_body=extra_body or None,
                stream=True,
                stream_options={"include_usage": True},
                **({"temperature": temperature} if temperature is not None else {}),
            ) as stream:
                for chunk in stream:
                    # The usage chunk has no choices: reading `choices[0]`
                    # first would raise on it.
                    _read_openai_usage(chunk, meta)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                    if reasoning:
                        yield Delta(reasoning=reasoning)
                    text = getattr(delta, "content", None)
                    if text:
                        yield Delta(content=text)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError,
                APIConnectionError) as exc:
            raise TransportUnavailable(str(exc)) from exc

    def followup_messages(self, base_messages: list[dict[str, Any]], reasoning_text: str,
                          *, model: str) -> list[dict[str, Any]]:
        tail = reasoning_text[-6000:] if len(reasoning_text) > 6000 else reasoning_text
        return self._refeed_as_user_turn(base_messages, tail)


def _read_openai_usage(chunk: Any, meta: StreamMeta) -> None:
    """Copy an OpenAI-protocol stream chunk's `usage` into `meta`. Asked for
    with `stream_options.include_usage`; the counts arrive on the final chunk
    (one with no choices), and a server that ignores the option sends none."""
    usage = getattr(chunk, "usage", None)
    if not usage:
        return
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt:
        meta.prompt_tokens = int(prompt)
    if completion:
        meta.completion_tokens = int(completion)


def _read_ollama_usage(part: Any, meta: StreamMeta) -> None:
    """Ollama's native final chunk: `prompt_eval_count` / `eval_count`."""
    prompt = getattr(part, "prompt_eval_count", None)
    completion = getattr(part, "eval_count", None)
    if prompt:
        meta.prompt_tokens = int(prompt)
    if completion:
        meta.completion_tokens = int(completion)


def _parse_input_token_overflow(msg: str) -> int | None:
    """Extract N from vLLM's 'prompt contains at least N input tokens' BadRequest.

    Regex-free replacement for ``prompt contains at least (\\d+) input tokens``:
    locate the head phrase, read the leading digit run, and require the exact
    ' input tokens' tail (so a partial/garbled message yields None, matching the
    old non-match → InputTooLong(None) behaviour). Hard Rule: no regex.
    """
    head = "prompt contains at least "
    tail = " input tokens"
    p = msg.find(head)
    if p == -1:
        return None
    start = p + len(head)
    i = start
    while i < len(msg) and msg[i].isdigit():
        i += 1
    if i == start or not msg[i:].startswith(tail):
        return None
    return int(msg[start:i])


class VLLMTransport(_BaseTransport):
    """vLLM via its OpenAI-compatible /v1/chat/completions endpoint.

    Reasoning tokens arrive in delta.reasoning_content (separate field), content in
    delta.content. Budgets are data-driven (see BudgetSpec); the controller adds the
    thinking reserve on top of the content budget and recovers length-truncation via
    followup.

    Environment variables:
      VLLM_BASE_URL, VLLM_MODEL_NAME, VLLM_MAX_MODEL_LEN, VLLM_THINKING_BUDGET,
      VLLM_CONTENT_BUDGET, VLLM_REPETITION_PENALTY, VLLM_REASONING_BUDGET,
      VLLM_MAX_THINK_CHARS

    VLLM_REASONING_BUDGET selects the reasoning shape (`llm/budget.py`):
    `fixed` (the default) reserves VLLM_THINKING_BUDGET tokens per call and
    breaks the stream at VLLM_MAX_THINK_CHARS; `per_call` gives each thinking
    call the whole context its prompt leaves and no character cap, and
    VLLM_THINKING_BUDGET is then only the planner's reserve — the floor the
    largest prompt still gets (`settings.safe_input_char_limit`).
    """

    supports_followup = True

    def __init__(self) -> None:
        super().__init__()
        self._base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
        self._model_name = os.getenv("VLLM_MODEL_NAME", MODEL)
        self.served_model = self._model_name
        self._ctx = int(os.getenv("VLLM_MAX_MODEL_LEN", "65536"))
        self._thinking_budget = int(os.getenv("VLLM_THINKING_BUDGET", "16384"))
        self._content_budget = int(os.getenv("VLLM_CONTENT_BUDGET", "16384"))
        self._rep_penalty = float(os.getenv("VLLM_REPETITION_PENALTY", "1.05"))
        self._per_call_reasoning = reasoning_budget_is_per_call(
            os.getenv("VLLM_REASONING_BUDGET", "fixed"))

    def count_input_tokens(self, messages: list[dict[str, Any]], *,
                           enable_thinking: bool) -> int | None:
        """vLLM's own `/tokenize` over the chat messages with the same
        template kwargs the request sends, so the count is the one the
        server validates against the context.

        Why: a 400 is the only other source and it carries no count. vLLM
        answers an over-long request with "prompt contains at least N input
        tokens" where N is ctx - max_tokens + 1, the threshold, never the
        prompt's size. Run `497151-a6fa205b` (2026-09-22 20:36 UTC): four
        calls opened together right after sub-2k-token prompts had moved
        the chars-per-token average from 3.51 to 3.61; `src/ensure.rs::new`
        was told "at least 31140", then 32531 (= 131072 - 98542 + 1), then
        33950 (= 131072 - 97123 + 1), and the third 400 ended the unit as
        `input too long` with no model answer. None on any failure: the
        estimate is still the fallback."""
        root = self._base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        payload = {
            "model": self._model_name, "messages": messages,
            "add_generation_prompt": True,
            "chat_template_kwargs": reasoning_template_kwargs(
                self._model_name, enable_thinking),
        }
        try:
            r = httpx.post(f"{root}/tokenize", json=payload, timeout=30.0)
            r.raise_for_status()
            count = r.json().get("count")
        except (httpx.HTTPError, ValueError):
            return None
        return int(count) if isinstance(count, int) and count > 0 else None

    def spec(self) -> BudgetSpec:
        return BudgetSpec(
            ctx_len=self._ctx, content_budget=self._content_budget,
            thinking_budget=self._thinking_budget, margin=512, cpt_floor=3.0,
            adaptive_cpt=True, add_thinking_to_request=True, cap_by_ctx=True,
            send_max_tokens=True, thinking_char_cap=_VLLM_MAX_THINK_CHARS,
            per_call_reasoning=self._per_call_reasoning,
        )

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        base = _model_uses_think(self._model_name) if want_think is None else want_think
        return base and self._thinking_budget > 0

    def _reasoning_delta(self, reasoning: str, enable_thinking: bool) -> Delta:
        """Which channel this server's reasoning-field text belongs on.

        A request that DISABLED thinking has no legitimate reasoning channel, so
        text arriving on one is the answer, misrouted by the server. Qwen3.6-FP8
        on vLLM does exactly that on the followup: `enable_thinking=False` sends
        the whole reply through `reasoning_content` with `content` left empty.
        Measured on the 2026-08-09 00:37 run — the followup capture in
        `logs/stream.file-first-stable-w1.log` holds four complete `<FN …>`
        blocks and carries NO `[CONTENT]` marker, which `_consume` writes for
        every content delta. All 35 followups that run logged `result_len=0`
        while the code sat in `cap.reasoning`, discarded as thinking; each of the
        13 `llm/think_cap` breaks fed one of them, so the cap bought a truncation
        and never a recovery.

        Yielding it as reasoning when thinking WAS requested stays correct — that
        is the case the separate field exists for. Scoped to this transport
        deliberately: it is a property of what this server does with
        `chat_template_kwargs.enable_thinking`, not a fact about the protocol,
        and the other compat paths are not in use to have been measured.

        Scoped to the boolean dialect for the same reason. A model whose template
        takes a reasoning STRENGTH has no disabled state to misroute out of —
        `enable_thinking=False` becomes the lowest rung, which still reasons, and
        its reasoning_content is then genuinely reasoning. Rerouting it to content
        would splice the model's thinking into the answer that gets written to a
        Rust body."""
        if _model_reasoning_dialect(self._model_name) == REASONING_STRENGTH:
            return Delta(reasoning=reasoning)
        return Delta(reasoning=reasoning) if enable_thinking else Delta(content=reasoning)

    def stream(self, messages: list[dict[str, Any]], *, model: str,
               max_tokens: int | None, enable_thinking: bool,
               meta: StreamMeta, temperature: float | None = None) -> Iterator[Delta]:
        # `Timeout` comes from the SDK, not from our own `httpx` import: openai
        # re-exports whichever httpx flavour it was built against, and the two are
        # not interchangeable. An SDK resolved on the httpx 2.x distribution
        # annotates this parameter as `httpx2._config.Timeout` and rejects the
        # `httpx._config.Timeout` a direct `httpx.Timeout(...)` builds — which is
        # how CI's mypy failed on 2026-08-15 against an unpinned openai while this
        # box was green. requirements-base.txt now pins the SDK; taking the type
        # from it as well keeps the line correct across a future bump.
        from openai import APIConnectionError, BadRequestError, OpenAI, Timeout
        client = OpenAI(base_url=self._base_url, api_key="vllm",
                        timeout=Timeout(connect=30, read=900, write=30, pool=30))
        extra_body: dict[str, Any] = {
            "chat_template_kwargs": reasoning_template_kwargs(self._model_name, enable_thinking),
            "repetition_penalty": self._rep_penalty,
        }
        try:
            with client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model_name,
                messages=messages,
                max_tokens=max_tokens,
                extra_body=extra_body,
                stream=True,
                stream_options={"include_usage": True},
                **({"temperature": temperature} if temperature is not None else {}),
            ) as stream:
                for chunk in stream:
                    _read_openai_usage(chunk, meta)
                    if not chunk.choices:
                        continue
                    _fr = chunk.choices[0].finish_reason
                    if _fr:
                        meta.finish_reason = _fr
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield self._reasoning_delta(reasoning, enable_thinking)
                    text = getattr(delta, "content", None)
                    if text:
                        yield Delta(content=text)
        except BadRequestError as exc:
            raise InputTooLong(_parse_input_token_overflow(str(exc))) from exc
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError,
                APIConnectionError) as exc:
            # The OpenAI SDK wraps httpx connection/timeout failures as its own
            # APIConnectionError (and APITimeoutError subclass), which would
            # otherwise escape this clause, bypass the controller's
            # wait_until_ready() readiness poll, and abort the run on a transient
            # vLLM restart/warmup. Route it through TransportUnavailable instead.
            raise TransportUnavailable(str(exc)) from exc

    def followup_messages(self, base_messages: list[dict[str, Any]], reasoning_text: str,
                          *, model: str) -> list[dict[str, Any]]:
        """Re-feed the reasoning and ask for code only.

        The reasoning goes in a USER turn. It used to go in an assistant turn as
        `<think>…</think>`, and Qwen3.6's chat template DISCARDS think blocks in
        prior assistant turns — so the followup re-fed nothing, then told the
        model "You have already reasoned through this problem above". Measured
        against the live server on 2026-08-09, prompt_tokens for that assistant
        turn against reasoning of 0 / 2,000 / 20,000 / 121,481 chars:

            33 / 33 / 33 / 33

        Thirty-three tokens of template scaffolding, whatever the size. The same
        bodies carried in a user turn cost 33 / 1,034 / 10,034 / 60,774. That is
        why the followup recovered essentially nothing: `logs/pipeline_guards.log`
        records **226 `llm/followup` fires and 225 with `result_len=0`**, and
        `thinking_len` did not separate the one success (120,188 chars) from the
        failures (median 113,731) — because none of it was ever sent.

        It also means the feared cost never existed: the followup prompt was the
        base prompt plus 33 tokens, not the ~220k chars / ~73k tokens it looked
        like from the client side. The trim below is what keeps the now-real
        payload inside ctx.
        """
        # Keep the whole chain when it fits; else trim from the FRONT (keep the
        # conclusion — the model's last thoughts are the ones nearest the code).
        base_chars = sum(len(m.get("content") or "") for m in base_messages)
        cpt = max(3.0, self._state.cpt)
        base_input = int(base_chars / cpt)
        think_tok_budget = max(512, self._ctx - base_input - self._content_budget - 512)
        think_char_budget = int(think_tok_budget * cpt)
        tail = (reasoning_text[-think_char_budget:]
                if len(reasoning_text) > think_char_budget else reasoning_text)
        return base_messages + [
            {"role": "user", "content": (
                "Your earlier reasoning on this exact task:\n\n"
                f"{tail}\n\n"
                "Now write the code block. Do not re-explain — output only the code."
            )},
        ]

    def wait_until_ready(self) -> None:
        import urllib.request as _ur
        _infer_url = self._base_url.rstrip("/") + "/chat/completions"
        _probe = json.dumps({"model": self._model_name,
                             "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 1}).encode()
        for _w in range(120):
            try:
                _req = _ur.Request(_infer_url, data=_probe,
                                   headers={"Content-Type": "application/json"})
                with _ur.urlopen(_req, timeout=10):
                    pass
                return
            except Exception:
                time.sleep(5)


def _persist_trace(result: str, thinking: str, suffix: str) -> None:
    """Write `thinking` to `logs/thinking/<sha256(result)><suffix>`, first
    writer wins. `result` is the string the call returned — the one
    `llm.response` records — so a reader reaches the trace from the event."""
    if not thinking:
        return
    try:
        import hashlib as _hl
        _think_dir = os.path.join(_LOG_DIR, "thinking")
        os.makedirs(_think_dir, exist_ok=True)
        _sha = _hl.sha256(result.encode("utf-8", "replace")).hexdigest()
        _tp = os.path.join(_think_dir, f"{_sha}{suffix}")
        if not os.path.exists(_tp):
            with open(_tp, "w", encoding="utf-8", errors="replace") as _tf:
                _tf.write(thinking)
    except Exception:
        pass


def _persist_thinking(result: str, thinking: str) -> None:
    """Persist the model's reasoning keyed by the response sha (callers read WHY)."""
    _persist_trace(result, thinking, ".txt")


def _persist_followup_thinking(result: str, thinking: str) -> None:
    """Persist the forced followup's OWN reasoning as `<sha>.followup.txt`,
    beside the main trace under the same response sha.

    Why not the main writer's key: `_persist_thinking` keeps the FIRST writer
    of `<sha>.txt`, and the controller persists the main trace before the
    followup's, so under one key the followup's would always lose — which is
    the record as it stood. Measured 2026-08-26 on run `82157-476ddc16`:
    1 595 of 1 596 recorded followup replies, and for 754 of them the file
    under the reply's sha held the MAIN trace (length equal to the stream
    row's `reasoning_chars`), i.e. the reasoning that wrote the reply on
    record was on disk nowhere. A distinct suffix gives it its own
    first-writer slot; the sha is shared so the two are siblings a reader
    finds from one `llm.response` row."""
    _persist_trace(result, thinking, ".followup.txt")


def _make_hooks() -> Hooks:
    return Hooks(
        sock_open=_sock_open,
        sock_write=_sock_write,
        sock_close=_sock_close,
        guard=_guard,
        dbg=_dbg,
        persist_thinking=_persist_thinking,
        persist_followup_thinking=_persist_followup_thinking,
        emit=lambda event, data: _emit_event(event, data=data),
    )


# ---------------------------------------------------------------------------
# Factory — selected once per process, reused for all ask() calls
# ---------------------------------------------------------------------------

def _backend_factory() -> Transport:
    backend_env = os.getenv("BACKEND", "").lower()
    if backend_env == "huggingface":
        return HuggingFaceTransport()
    if backend_env == "sglang":
        return SGLangTransport()
    if backend_env == "vllm":
        return VLLMTransport()
    return OllamaTransport()
