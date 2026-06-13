#!/usr/bin/env python3
"""
Send a prompt to a language model and return the response.

Backend is selected via the BACKEND environment variable (or .env file):
  - BACKEND=huggingface                                     → HuggingFace transformers
  - BACKEND=vllm / sglang                                   → vLLM / SGLang OpenAI API
  - BACKEND=ollama        (default)                         → Ollama local API

This pipeline is self-hosted only (Hard Rule 7): no external/cloud LLM backend.

Usage:
  python model_interact.py "your prompt here"
  python model_interact.py --file prompt.txt
  echo "prompt" | python model_interact.py
  python model_interact.py "prompt" --output response.txt
  python model_interact.py "prompt" --system "system prompt"
  python model_interact.py "prompt" --model qwen2.5-coder:7b
"""

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from pipeline_core.llm import (
    BudgetSpec,
    BudgetState,
    Delta,
    Hooks,
    InputTooLong,
    LLMController,
    StreamMeta,
    Transport,
    TransportTimeout,
    TransportUnavailable,
)
from pipeline_core.llm.cleanup import strip_chat_boundaries, strip_think
from pipeline_core.llm.thinktag import InlineThinkSplitter

load_dotenv(Path(__file__).parent / ".env")

# Model is read from .env (MODEL=...) or falls back to this default.
# For Ollama: MODEL=qwen2.5-coder:7b
# For HuggingFace: set BACKEND=huggingface and HF_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
MODEL = os.getenv("MODEL", "qwen2.5-coder:7b")

def _default_max_tokens() -> int:
    if os.getenv("MAX_TOKENS"):
        return int(os.getenv("MAX_TOKENS"))
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
# History:
#   qwen3:32b        — confirmed good results via OpenAI-compat + think:true (sessions 3-9)
#   qwen3.6:27b      — needs native ollama endpoint; think:true recommended for coding tasks
#   qwen3.6-27b-q8   — SGLang served name for the same model; uses OpenAI-compat + thinking_budget
MODEL_CAPS: dict[str, dict] = {
    "qwen3:32b":                  {"think": True,  "native_ollama": False},
    "qwen3.6:27b":                {"think": True,  "native_ollama": True},
    "qwen3.6:27b-q8":             {"think": False, "native_ollama": False},   # compat layer routes think=True to reasoning_content (empty content); think=False works reliably
    "qwen3.6-27b-q8":             {"think": True,  "native_ollama": False},   # SGLang served name (dash, not colon)
    "Qwen/Qwen3.6-27B-FP8":                      {"think": True,  "native_ollama": False},
    "mattbucci/Qwen3.6-27B-AWQ":                  {"think": True,  "native_ollama": False},
    "/home/user/Qwen3.6-27B-Q8_0.gguf":          {"think": True,  "native_ollama": False},
}


def _model_uses_think(model: str) -> bool:
    return bool(MODEL_CAPS.get(model, {}).get("think", True))


def _model_use_native_ollama(model: str) -> bool:
    return bool(MODEL_CAPS.get(model, {}).get("native_ollama", False))


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

    def followup_messages(self, base_messages: list[dict], reasoning_text: str,
                          *, model: str) -> list[dict]:
        return base_messages

    def clean(self, text: str, *, model: str) -> str:
        return strip_think(text)

    def wait_until_ready(self) -> None:
        return None


_LOG_DIR          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_DEBUG_LOG        = os.getenv("LLM_DEBUG_LOG", os.getenv("OLLAMA_DEBUG_LOG", os.path.join(_LOG_DIR, "debug.log")))
_STREAM_LOG       = os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG", os.path.join(_LOG_DIR, "stream.log")))
# Silence timeout: if no token arrives for this many seconds, treat as stalled.
_CHUNK_TIMEOUT    = int(os.getenv("OLLAMA_CHUNK_TIMEOUT", "60"))
# Thinking cap: break out of thinking loop after this many chars (~10k tokens).
# Falls through to the existing followup "now write the code" path.
_MAX_THINK_CHARS  = int(os.getenv("OLLAMA_MAX_THINK_CHARS", "40000"))


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


def _timed_iter(it, timeout: int):
    """Wrap an iterator; raise TimeoutError if no item arrives within `timeout` seconds."""
    import queue as _q
    import threading as _t
    q: _q.Queue[object] = _q.Queue()
    sentinel = object()

    def _feed():
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

    def stream(self, messages, *, model, max_tokens, enable_thinking, meta: StreamMeta):
        if _model_use_native_ollama(model):
            yield from self._stream_native(messages, model, enable_thinking)
        else:
            yield from self._stream_compat(messages, model, max_tokens, enable_thinking)

    def _stream_native(self, messages, model, enable_thinking):
        import ollama as _ollama
        try:
            for part in _timed_iter(
                _ollama.chat(model=model, messages=messages, think=enable_thinking,
                             stream=True, options={"num_ctx": self._num_ctx}),
                _CHUNK_TIMEOUT,
            ):
                thinking = getattr(part.message, "thinking", None)
                if thinking:
                    yield Delta(reasoning=thinking)
                delta = part.message.content
                if delta:
                    yield Delta(content=delta)
        except TimeoutError as exc:
            raise TransportTimeout(str(exc)) from exc

    def _stream_compat(self, messages, model, max_tokens, enable_thinking):
        from openai import OpenAI
        client = OpenAI(base_url=self._base_url, api_key="ollama", timeout=300)
        # Qwen3 compat layer has no think param — /no_think suppresses reasoning.
        if not enable_thinking and messages and messages[-1]["role"] == "user":
            messages = messages[:-1] + [
                {**messages[-1], "content": "/no_think " + messages[-1]["content"]}
            ]
        splitter = InlineThinkSplitter()
        with client.chat.completions.create(  # type: ignore[union-attr]
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            extra_body={"options": {"num_ctx": self._num_ctx}},
            stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if not delta:
                    continue
                c, r = splitter.feed(delta)
                if r:
                    yield Delta(reasoning=r)
                if c:
                    yield Delta(content=c)
        c, r = splitter.finish()
        if r:
            yield Delta(reasoning=r)
        if c:
            yield Delta(content=c)

    def followup_messages(self, base_messages, reasoning_text, *, model):
        if _model_use_native_ollama(model):
            # Native endpoint accepts the assistant `thinking` field directly.
            return base_messages + [
                {"role": "assistant", "thinking": reasoning_text, "content": ""},
                {"role": "user", "content": (
                    "You have already reasoned through this problem above. "
                    "Now write the code block. Do not re-explain — output only the code."
                )},
            ]
        tail = reasoning_text[-6000:] if len(reasoning_text) > 6000 else reasoning_text
        return base_messages + [
            {"role": "assistant", "content": f"<think>\n{tail}\n</think>"},
            {"role": "user", "content": (
                "You have already reasoned through this problem above. "
                "Now write the code block. Do not re-explain — output only the code."
            )},
        ]

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

    def _load(self):
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        dtype_map = {"bfloat16": torch.bfloat16, "float16": torch.float16, "auto": "auto"}
        torch_dtype = dtype_map.get(self._torch_dtype_str, torch.bfloat16)

        quantization_config = None
        if self._load_in_4bit or self._load_in_8bit:
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
              f"{ {str(d) for d in self._model.hf_device_map.values()} }", file=sys.stderr)  # type: ignore[operator]

    def stream(self, messages, *, model, max_tokens, enable_thinking, meta: StreamMeta):
        self._load()
        import torch

        text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        inputs = self._tokenizer([text], return_tensors="pt").to(self._model.device)
        with torch.no_grad():
            output_ids = self._model.generate(  # type: ignore[operator]
                **inputs,
                max_new_tokens=min(max_tokens, self._max_new_tokens),
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        new_ids = output_ids[0][inputs["input_ids"].shape[1]:]
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

    def stream(self, messages, *, model, max_tokens, enable_thinking, meta: StreamMeta):
        from openai import OpenAI
        client = OpenAI(base_url=self._base_url, api_key="sglang", timeout=120)
        extra_body: dict = {}
        if enable_thinking and self._thinking_budget > 0:
            extra_body["thinking_budget"] = self._thinking_budget
        with client.chat.completions.create(  # type: ignore[union-attr]
            model=self._model_name,
            messages=messages,
            max_tokens=max_tokens,
            extra_body=extra_body or None,
            stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    yield Delta(reasoning=reasoning)
                text = getattr(delta, "content", None)
                if text:
                    yield Delta(content=text)

    def followup_messages(self, base_messages, reasoning_text, *, model):
        tail = reasoning_text[-6000:] if len(reasoning_text) > 6000 else reasoning_text
        return base_messages + [
            {"role": "assistant", "content": f"<think>\n{tail}\n</think>"},
            {"role": "user", "content": (
                "You have already reasoned through this problem above. "
                "Now write the code block. Do not re-explain — output only the code."
            )},
        ]


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
      VLLM_CONTENT_BUDGET, VLLM_REPETITION_PENALTY
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

    def spec(self) -> BudgetSpec:
        return BudgetSpec(
            ctx_len=self._ctx, content_budget=self._content_budget,
            thinking_budget=self._thinking_budget, margin=512, cpt_floor=3.0,
            adaptive_cpt=True, add_thinking_to_request=True, cap_by_ctx=True,
            send_max_tokens=True, thinking_char_cap=None,
        )

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        base = _model_uses_think(self._model_name) if want_think is None else want_think
        return base and self._thinking_budget > 0

    def stream(self, messages, *, model, max_tokens, enable_thinking, meta: StreamMeta):
        from openai import BadRequestError, OpenAI
        client = OpenAI(base_url=self._base_url, api_key="vllm",
                        timeout=httpx.Timeout(connect=30, read=900, write=30, pool=30))
        extra_body = {
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
            "repetition_penalty": self._rep_penalty,
        }
        try:
            with client.chat.completions.create(  # type: ignore[call-overload,union-attr]
                model=self._model_name,
                messages=messages,
                max_tokens=max_tokens,
                extra_body=extra_body,
                stream=True,
                stream_options={"include_usage": True},
            ) as stream:
                for chunk in stream:
                    if chunk.usage and chunk.usage.prompt_tokens:
                        meta.prompt_tokens = chunk.usage.prompt_tokens
                    if not chunk.choices:
                        continue
                    _fr = chunk.choices[0].finish_reason
                    if _fr:
                        meta.finish_reason = _fr
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield Delta(reasoning=reasoning)
                    text = getattr(delta, "content", None)
                    if text:
                        yield Delta(content=text)
        except BadRequestError as exc:
            raise InputTooLong(_parse_input_token_overflow(str(exc))) from exc
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            raise TransportUnavailable(str(exc)) from exc

    def followup_messages(self, base_messages, reasoning_text, *, model):
        # Keep the whole chain when it fits; else trim from the FRONT (keep the conclusion).
        base_chars = sum(len(m.get("content") or "") for m in base_messages)
        cpt = max(3.0, self._state.cpt)
        base_input = int(base_chars / cpt)
        think_tok_budget = max(512, self._ctx - base_input - self._content_budget - 512)
        think_char_budget = int(think_tok_budget * cpt)
        tail = (reasoning_text[-think_char_budget:]
                if len(reasoning_text) > think_char_budget else reasoning_text)
        return base_messages + [
            {"role": "assistant", "content": f"<think>\n{tail}\n</think>"},
            {"role": "user", "content": (
                "You have already reasoned through this problem above. "
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


def _persist_thinking(result: str, thinking: str) -> None:
    """Persist the model's reasoning keyed by the response sha (callers read WHY)."""
    if not thinking:
        return
    try:
        import hashlib as _hl
        _think_dir = os.path.join(_LOG_DIR, "thinking")
        os.makedirs(_think_dir, exist_ok=True)
        _sha = _hl.sha256(result.encode("utf-8", "replace")).hexdigest()
        _tp = os.path.join(_think_dir, f"{_sha}.txt")
        if not os.path.exists(_tp):
            with open(_tp, "w", encoding="utf-8", errors="replace") as _tf:
                _tf.write(thinking)
    except Exception:
        pass


def _make_hooks() -> Hooks:
    return Hooks(
        sock_open=_sock_open,
        sock_write=_sock_write,
        sock_close=_sock_close,
        guard=_guard,
        dbg=_dbg,
        persist_thinking=_persist_thinking,
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


_backend: Transport | None = None
_controller: LLMController | None = None


def _get_backend() -> Transport:
    global _backend
    if _backend is None:
        _backend = _backend_factory()
    return _backend


def _get_controller() -> LLMController:
    global _controller
    if _controller is None:
        _controller = LLMController(_make_hooks(), default_sentinel=MAX_TOKENS)
    return _controller


# ---------------------------------------------------------------------------
# Public API — unchanged signature; pipeline.py needs no edits
# ---------------------------------------------------------------------------

def ask(prompt: str, system: str | None = None, model: str | None = None,
        max_tokens: int = MAX_TOKENS, think: bool | None = None) -> str:
    if model is None:
        model = MODEL
    return _get_controller().run(
        _get_backend(), system=system, prompt=prompt, model=model,
        max_tokens=max_tokens, want_think=think,
    )


def main():
    parser = argparse.ArgumentParser(description="Send a prompt to a language model.")
    parser.add_argument("prompt", nargs="?", help="Prompt text (or omit to read from stdin)")
    parser.add_argument("--file", help="Read prompt from this file instead")
    parser.add_argument("--output", help="Write response to this file instead of stdout")
    parser.add_argument("--system", help="Optional system prompt")
    parser.add_argument("--model", default=MODEL, help=f"Model to use (default: {MODEL})")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            prompt = f.read()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()

    response = ask(
        prompt,
        system=args.system,
        model=args.model,
        max_tokens=args.max_tokens,
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(response)
        print(f"Response written to {args.output}", file=sys.stderr)
    else:
        print(response)


if __name__ == "__main__":
    main()
