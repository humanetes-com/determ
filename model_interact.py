#!/usr/bin/env python3
"""
Send a prompt to a language model and return the response.

Backend is selected via the BACKEND environment variable (or .env file):
  - BACKEND=claude        (or MODEL starting with "claude") → Anthropic API
  - BACKEND=huggingface                                     → HuggingFace transformers
  - BACKEND=ollama        (default)                         → Ollama local API

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
import time
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Model is read from .env (MODEL=...) or falls back to this default.
# For Ollama: MODEL=qwen2.5-coder:7b
# For Claude: MODEL=claude-haiku-4-5-20251001
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
# Adaptive chars-per-token ratio for vLLM input token estimation.
#
# The vLLM backend requests usage stats (prompt_tokens) after every call and
# updates this EWM. Estimation uses the learned ratio with a hard floor of 2.0
# chars/token so the estimate never becomes dangerously optimistic.
# ---------------------------------------------------------------------------
_vllm_cpt: float = 2.0        # current EWM estimate of chars per input token
_vllm_cpt_alpha: float = 0.15  # EWM smoothing — low to avoid over-reacting to one outlier


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    @abstractmethod
    def ask(self, prompt: str, system: str | None, model: str, max_tokens: int,
            think: bool | None = None) -> str: ...


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------

class ClaudeBackend(LLMBackend):
    def ask(self, prompt: str, system: "str | None", model: str, max_tokens: int,
            think: "bool | None" = None) -> str:
        import anthropic
        client = anthropic.Anthropic()
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as stream:  # type: ignore[arg-type]
            block = stream.get_final_message().content[0]
            return block.text if hasattr(block, "text") else ""  # type: ignore[union-attr]


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
_stream_fh = None


def _sock_open(label: str = "") -> None:
    """Open the stream log for this call (overwrite). Silently skip on error."""
    global _stream_fh
    import time as _time
    if _stream_fh:
        with contextlib.suppress(Exception):
            _stream_fh.close()
    if not _STREAM_LOG:
        _stream_fh = None
        return
    try:
        _stream_fh = open(_STREAM_LOG, "w", buffering=1)  # line-buffered flush
        _stream_fh.write(f"[CALL_START: {_time.time():.3f}]\n")
        if label:
            _stream_fh.write(f"[CALL: {label}]\n")
        _stream_fh.flush()
    except Exception:
        _stream_fh = None


def _sock_write(text: str) -> None:
    if not _stream_fh or not text:
        return
    try:
        _stream_fh.write(text)
        _stream_fh.flush()
    except Exception:
        pass


def _sock_close() -> None:
    global _stream_fh
    if _stream_fh:
        with contextlib.suppress(Exception):
            _stream_fh.close()
        _stream_fh = None


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


class OllamaBackend(LLMBackend):
    def __init__(self):
        self._base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self._num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    def ask(self, prompt, system, model, max_tokens, think=None):
        from openai import OpenAI
        # timeout=120 is a per-chunk read timeout, not a total-generation timeout.
        # With streaming, tokens arrive continuously so this never fires during active
        # generation — it only fires if the connection goes dead (no token for 2 min).
        client = OpenAI(base_url=self._base_url, api_key="ollama", timeout=300)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # think=None means use the model's default from MODEL_CAPS.
        # Callers can pass think=False to suppress reasoning on simple fill-in tasks.
        _think = _model_uses_think(model) if think is None else think

        # Qwen3.6+ requires the native /api/chat endpoint — the OpenAI-compat layer
        # routes thinking tokens into reasoning_content, leaving content empty.
        chunks: list[str] = []
        followup_chunks: list[str] = []
        if _model_use_native_ollama(model):
            import ollama as _ollama
            chunks = []
            thinking_chunks: list[str] = []
            _sock_open(f"model={model} think={_think}")
            saw_content = False
            _nat_cbuf = ""
            _nat_blk_tracking = False
            _nat_blk_depth = 0
            _nat_file_opens = 0
            _nat_file_closes = 0
            try:
                for part in _timed_iter(
                    _ollama.chat(
                        model=model,
                        messages=messages,
                        think=_think,
                        stream=True,
                        options={"num_ctx": self._num_ctx},
                    ),
                    _CHUNK_TIMEOUT,
                ):
                    delta = part.message.content
                    if delta:
                        if not saw_content:
                            _sock_write("\n[CONTENT]\n")
                            saw_content = True
                        chunks.append(delta)
                        _sock_write(delta)
                        _nat_cbuf += delta
                        # Stop when mod gen_* block closes.
                        if not _nat_blk_tracking:
                            _mg = _nat_cbuf.find('mod gen_')
                            if _mg != -1:
                                _ob = _nat_cbuf.find('{', _mg)
                                if _ob != -1:
                                    _nat_blk_tracking = True
                                    for _ch in _nat_cbuf[_ob:]:
                                        if _ch == '{': _nat_blk_depth += 1
                                        elif _ch == '}': _nat_blk_depth -= 1
                                    if _nat_blk_depth <= 0:
                                        _sock_write("\n[BLOCK_CLOSED]\n")
                                        _guard("native/block_closed",
                                               f"model={model} mod gen_* closed at {len(_nat_cbuf)} chars")
                                        break
                        elif _nat_blk_tracking:
                            for _ch in delta:
                                if _ch == '{': _nat_blk_depth += 1
                                elif _ch == '}': _nat_blk_depth -= 1
                            if _nat_blk_depth <= 0:
                                _sock_write("\n[BLOCK_CLOSED]\n")
                                _guard("native/block_closed",
                                       f"model={model} mod gen_* closed at {len(_nat_cbuf)} chars")
                                break
                        # Stop when all <FILE ...>...</FILE> blocks are closed.
                        _nat_file_opens  += delta.count('<FILE ')
                        _nat_file_closes += delta.count('</FILE>')
                        if _nat_file_opens > 0 and _nat_file_opens == _nat_file_closes:
                            _sock_write("\n[FILE_CLOSED]\n")
                            _guard("native/file_closed",
                                   f"model={model} {_nat_file_opens} FILE block(s) closed at "
                                   f"{len(_nat_cbuf)} chars")
                            break
                    thinking = getattr(part.message, "thinking", None)
                    if thinking:
                        thinking_chunks.append(thinking)
                        _sock_write(thinking)
                        if sum(len(t) for t in thinking_chunks) >= _MAX_THINK_CHARS:
                            _dbg(f"THINK_CAP hit at {_MAX_THINK_CHARS} chars — forcing followup")
                            _sock_write(f"\n[THINK_CAP: {_MAX_THINK_CHARS} chars — requesting code]\n")
                            break
            except TimeoutError as exc:
                _dbg(f"CHUNK_TIMEOUT model={model} think={_think} "
                     f"content_chars={sum(len(c) for c in chunks)} "
                     f"thinking_chars={sum(len(t) for t in thinking_chunks)} | {exc}")
                _sock_write(f"\n[TIMEOUT: {exc}]\n")
                _sock_close()
                return ""

            result = "".join(chunks)

            # If content is empty but thinking exists, the model exhausted its output
            # budget reasoning. Pass the thinking back as an assistant turn and ask it
            # to now write the code — avoids re-doing all the reasoning from scratch.
            if not result.strip() and thinking_chunks:
                followup_messages = messages + [
                    {"role": "assistant", "thinking": "".join(thinking_chunks), "content": ""},
                    {"role": "user", "content": (
                        "You have already reasoned through this problem above. "
                        "Now write the code block. Do not re-explain — output only the code."
                    )},
                ]
                followup_chunks = []
                _sock_write("\n[FOLLOWUP — thinking budget exhausted, requesting code]\n")
                try:
                    for part in _timed_iter(
                        _ollama.chat(
                            model=model,
                            messages=followup_messages,
                            think=False,
                            stream=True,
                            options={"num_ctx": self._num_ctx},
                        ),
                        _CHUNK_TIMEOUT,
                    ):
                        delta = part.message.content
                        if delta:
                            followup_chunks.append(delta)
                            _sock_write(delta)
                except TimeoutError as exc:
                    _dbg(f"CHUNK_TIMEOUT(followup) model={model} | {exc}")
                    _sock_write(f"\n[TIMEOUT: {exc}]\n")
                    _sock_close()
                    return ""
                result = "".join(followup_chunks)

            _sock_close()
            return result

        # For Qwen3 hybrid-thinking models: prepend /no_think when caller requests
        # think=False. The compat layer has no native think parameter, but Qwen3
        # recognises /no_think at the start of the user message to skip reasoning.
        if not _think and messages and messages[-1]["role"] == "user":
            messages = messages[:-1] + [
                {**messages[-1], "content": "/no_think " + messages[-1]["content"]}
            ]

        _msg_chars = sum(len(m.get("content") or "") for m in messages)

        # Cap max_tokens so input + output never exceeds num_ctx.
        # Ollama generates an immediate EOS when max_tokens would overflow the KV cache.
        _estimated_input_tokens = max(1, int(_msg_chars / 3.5))
        _safe_max_tokens = max(512, self._num_ctx - _estimated_input_tokens - 256)
        if max_tokens > _safe_max_tokens:
            _guard("compat/max_tokens_cap",
                   f"model={model} max_tokens {max_tokens} → {_safe_max_tokens} "
                   f"(input ~{_estimated_input_tokens} tok, num_ctx={self._num_ctx})")
        max_tokens = min(max_tokens, _safe_max_tokens)

        _sock_open(f"model={model} think={_think}")
        _dbg(f"CALL model={model} think={_think} input_chars={_msg_chars} "
             f"est_input_tok={_estimated_input_tokens} max_tokens={max_tokens}")
        chunks = []
        _in_think = False
        _think_char_count = 0
        _tail = ""  # trailing chars of last chunk, for boundary-spanning token detection
        # Structural stop detectors — no-ops when the expected tags aren't present.
        _cbuf = ""           # accumulated non-thinking content
        _blk_tracking = False
        _blk_depth = 0
        _file_opens = 0
        _file_closes = 0
        with client.chat.completions.create(  # type: ignore[union-attr]
            model=model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            extra_body={"options": {"num_ctx": self._num_ctx}},
            stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    chunks.append(delta)
                    _sock_write(delta)
                    _window = _tail + delta
                    if not _in_think and "<think>" in _window:
                        _in_think = True
                    if _in_think:
                        _think_char_count += len(delta)
                        if "</think>" in _window:
                            _in_think = False
                            _think_char_count = 0
                        elif _think_char_count > _MAX_THINK_CHARS:
                            _guard("compat/think_cap",
                                   f"model={model} broke stream at {_think_char_count} thinking chars "
                                   f"(cap={_MAX_THINK_CHARS}) — followup will extract code")
                            _sock_write(f"\n[THINK_CAP: {_MAX_THINK_CHARS} chars]\n")
                            break
                    else:
                        # Track braces only in the content (non-thinking) part.
                        _cbuf += delta
                        if not _blk_tracking:
                            _mg = _cbuf.find('mod gen_')
                            if _mg != -1:
                                _ob = _cbuf.find('{', _mg)
                                if _ob != -1:
                                    _blk_tracking = True
                                    for _ch in _cbuf[_ob:]:
                                        if _ch == '{': _blk_depth += 1
                                        elif _ch == '}': _blk_depth -= 1
                                    if _blk_depth <= 0:
                                        _sock_write("\n[BLOCK_CLOSED]\n")
                                        _guard("compat/block_closed",
                                               f"model={model} mod gen_* closed at "
                                               f"{len(_cbuf)} content chars")
                                        break
                        else:
                            for _ch in delta:
                                if _ch == '{': _blk_depth += 1
                                elif _ch == '}': _blk_depth -= 1
                            if _blk_depth <= 0:
                                _sock_write("\n[BLOCK_CLOSED]\n")
                                _guard("compat/block_closed",
                                       f"model={model} mod gen_* closed at "
                                       f"{len(_cbuf)} content chars")
                                break
                        # Stop when all <FILE ...>...</FILE> blocks are closed.
                        _file_opens  += delta.count('<FILE ')
                        _file_closes += delta.count('</FILE>')
                        if _file_opens > 0 and _file_opens == _file_closes:
                            _sock_write("\n[FILE_CLOSED]\n")
                            _guard("compat/file_closed",
                                   f"model={model} {_file_opens} FILE block(s) closed at "
                                   f"{len(_cbuf)} chars")
                            break
                    _tail = delta[-15:]  # keep enough to detect tokens spanning chunks
        result = "".join(chunks)
        _dbg(f"RAW model={model} raw_len={len(result)} max_tokens={max_tokens} "
             f"raw_start={result[:120]!r}")

        # Strip <think>...</think> blocks — returned as raw content via compat layer.
        # Strip chat-format boundary tokens individually — do NOT truncate at the first
        # occurrence. The model sometimes emits <|endoftext|><|im_start|>N. between
        # multiple candidate blocks; truncating would discard the actual code block.
        import re as _re
        thinking_match = _re.search(r'<think>(.*?)</think>', result, flags=_re.DOTALL)
        thinking_text = thinking_match.group(1).strip() if thinking_match else ""
        result = _re.sub(r'<think>.*?</think>\s*', '', result, flags=_re.DOTALL)
        # Truncate at EOS if followed by a role turn — model echoing user/system prompt back.
        # But keep content when followed by numbered alternatives (<|im_start|>1. etc.).
        _before_role_trunc = result
        result = _re.sub(r'<\|endoftext\|>\s*<\|im_start\|>(user|system|assistant)\b.*', '', result, flags=_re.DOTALL)
        if result != _before_role_trunc:
            _guard("compat/endoftext_role_echo",
                   f"model={model} truncated at <|endoftext|><|im_start|>role — "
                   f"dropped {len(_before_role_trunc) - len(result):,} chars of echoed prompt")
        # Truncate at EOS when followed by numbered alternatives (1., 2., …).
        # Stripping headers but keeping bodies concatenates alternatives, corrupting responses.
        _before_num_trunc = result
        result = _re.sub(r'<\|endoftext\|>\s*<\|im_start\|>\s*\d+\..*', '', result, flags=_re.DOTALL)
        if result != _before_num_trunc:
            _guard("compat/endoftext_stripped",
                   f"model={model} truncated at first EOS (numbered alternatives detected — "
                   f"dropped {len(_before_num_trunc) - len(result):,} chars)")
        else:
            # Strip remaining bare EOS and other im_start/im_end tokens.
            _eos_count = result.count('<|endoftext|>')
            _im_count = result.count('<|im_start|>')
            result = _re.sub(r'<\|endoftext\|>', '', result)
            result = _re.sub(r'<\|im_start\|>[^\n]*\n?', '', result)
            result = _re.sub(r'<\|im_end\|>', '', result)
            if _eos_count or _im_count:
                _guard("compat/endoftext_stripped",
                       f"model={model} stripped {_eos_count} EOS + {_im_count} im_start tokens "
                       f"(bare boundaries)")
        result = result.strip()

        # Detect unclosed <think> block — stream broken by THINK_CAP or max_tokens hit
        # mid-reasoning. The stripping regex only removes complete pairs, so result still
        # holds the raw <think>... text. Extract the partial reasoning so the follow-up fires.
        if not thinking_text and '<think>' in result and '</think>' not in result:
            _dbg(f"UNCLOSED_THINK model={model} think={_think} input_chars={_msg_chars} "
                 f"raw_len={len(result)}")
            thinking_text = result[result.find('<think>') + 7:].strip()
            result = ""
        elif not result and not thinking_text:
            # Model returned nothing at all — no thinking, no code.
            _dbg(f"EMPTY_RESPONSE model={model} think={_think} input_chars={_msg_chars}")
        elif not result and thinking_text:
            pass  # normal thinking-only path — follow-up will log below

        # If the model exhausted its output budget on reasoning and produced no code,
        # send a follow-up with the thinking as plain assistant content so it can
        # continue without re-reasoning. The compat layer doesn't support the native
        # {"role": "assistant", "thinking": ...} field, so we embed it in content.
        # /no_think prevents the model from opening a new reasoning chain on the follow-up.
        if not result and thinking_text:
            _dbg(f"FOLLOWUP model={model} think={_think} input_chars={_msg_chars} "
                 f"thinking_len={len(thinking_text)}")
            _sock_write(f"\n[FOLLOWUP — thinking budget exhausted ({len(thinking_text)} chars), requesting code]\n")
            # Cap thinking embedded in follow-up — only the final reasoning matters,
            # not the full chain. Large thinking blocks (possible with 155K max_tokens)
            # would make the follow-up context enormous and re-trigger deep reasoning.
            thinking_tail = thinking_text[-6000:] if len(thinking_text) > 6000 else thinking_text
            followup_messages = messages + [
                {"role": "assistant", "content": f"<think>\n{thinking_tail}\n</think>"},
                {"role": "user", "content": (
                    "/no_think You have already reasoned through this problem above. "
                    "Now write the code block. Do not re-explain — output only the code."
                )},
            ]
            # Follow-up only needs to produce code, not re-reason — cap at 16K tokens.
            followup_max = min(max_tokens, 16384)
            followup_chunks = []
            _fu_cbuf = ""
            _fu_blk_tracking = False
            _fu_blk_depth = 0
            with client.chat.completions.create(  # type: ignore[union-attr]
                model=model,
                messages=followup_messages,  # type: ignore[arg-type]
                max_tokens=followup_max,
                extra_body={"options": {"num_ctx": self._num_ctx}},
                stream=True,
            ) as stream:
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        followup_chunks.append(delta)
                        _sock_write(delta)
                        _fu_cbuf += delta
                        if not _fu_blk_tracking:
                            _mg = _fu_cbuf.find('mod gen_')
                            if _mg != -1:
                                _ob = _fu_cbuf.find('{', _mg)
                                if _ob != -1:
                                    _fu_blk_tracking = True
                                    for _ch in _fu_cbuf[_ob:]:
                                        if _ch == '{': _fu_blk_depth += 1
                                        elif _ch == '}': _fu_blk_depth -= 1
                                    if _fu_blk_depth <= 0:
                                        _sock_write("\n[BLOCK_CLOSED]\n")
                                        _guard("compat/block_closed",
                                               f"model={model} followup mod gen_* closed at "
                                               f"{len(_fu_cbuf)} chars")
                                        break
                        else:
                            for _ch in delta:
                                if _ch == '{': _fu_blk_depth += 1
                                elif _ch == '}': _fu_blk_depth -= 1
                            if _fu_blk_depth <= 0:
                                _sock_write("\n[BLOCK_CLOSED]\n")
                                _guard("compat/block_closed",
                                       f"model={model} followup mod gen_* closed at "
                                       f"{len(_fu_cbuf)} chars")
                                break
            result = "".join(followup_chunks)
            result = _re.sub(r'<think>.*?</think>\s*', '', result, flags=_re.DOTALL)
            _fu_before = result
            result = _re.sub(r'<\|endoftext\|>\s*<\|im_start\|>(user|system|assistant)\b.*', '', result, flags=_re.DOTALL)
            if result != _fu_before:
                _guard("compat/followup_role_echo",
                       f"model={model} followup truncated at role turn — "
                       f"dropped {len(_fu_before) - len(result):,} chars")
            result = _re.sub(r'<\|endoftext\|>', '', result)
            result = _re.sub(r'<\|im_start\|>[^\n]*\n?', '', result)
            result = _re.sub(r'<\|im_end\|>', '', result)
            result = result.strip()
            if not result:
                _dbg_response("FOLLOWUP_ALSO_EMPTY raw", "".join(followup_chunks))

        _sock_close()
        return result


class HuggingFaceBackend(LLMBackend):
    """
    Local inference via HuggingFace transformers + accelerate.
    Model is loaded once on first call and reused for all subsequent calls.

    Note: the `model` parameter passed to ask() is ignored — the loaded model
    is fixed at init time. Per-task model overrides in pipeline.py have no effect
    when using this backend. Set HF_MODEL once for all tasks.

    Environment variables:
      HF_MODEL          HuggingFace repo ID, e.g. Qwen/Qwen2.5-Coder-32B-Instruct
      HF_DEVICE_MAP     Device placement strategy (default: auto)
      HF_LOAD_IN_4BIT   4-bit quantization via bitsandbytes (default: false)
      HF_LOAD_IN_8BIT   8-bit quantization via bitsandbytes (default: false)
      HF_TORCH_DTYPE    bfloat16 | float16 | auto (default: bfloat16)
      HF_MAX_NEW_TOKENS Max tokens to generate (default: 4096)
    """

    def __init__(self):
        self._hf_model_id = os.getenv("HF_MODEL", MODEL)
        self._device_map = os.getenv("HF_DEVICE_MAP", "auto")
        self._load_in_4bit = os.getenv("HF_LOAD_IN_4BIT", "false").lower() == "true"
        self._load_in_8bit = os.getenv("HF_LOAD_IN_8BIT", "false").lower() == "true"
        self._torch_dtype_str = os.getenv("HF_TORCH_DTYPE", "bfloat16")
        self._max_new_tokens = int(os.getenv("HF_MAX_NEW_TOKENS", str(MAX_TOKENS)))
        self._model = None
        self._tokenizer = None

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

        print(f"[HuggingFaceBackend] Loading {self._hf_model_id} ...", file=sys.stderr)
        self._tokenizer = AutoTokenizer.from_pretrained(self._hf_model_id)
        self._model = AutoModelForCausalLM.from_pretrained(
            self._hf_model_id,
            device_map=self._device_map,
            torch_dtype=torch_dtype,
            quantization_config=quantization_config,
        )
        print(f"[HuggingFaceBackend] Model loaded on: "
              f"{ {str(d) for d in self._model.hf_device_map.values()} }", file=sys.stderr)  # type: ignore[operator]

    def ask(self, prompt: str, system: "str | None", model: str, max_tokens: int,
            think: "bool | None" = None) -> str:
        self._load()
        import torch

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

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
        return str(self._tokenizer.decode(new_ids, skip_special_tokens=True))


class SGLangBackend(LLMBackend):
    """
    SGLang server via its OpenAI-compatible /v1/chat/completions endpoint.

    SGLang advantages over Ollama for this pipeline:
      - RadixAttention: trie-based token-level KV cache reuse. The shared
        system prompt (~40k chars) across all TASK 0c attempts is cached and
        never recomputed — fix-loop calls are ~3× faster.
      - thinking_budget parameter: caps per-request reasoning tokens. Prevents
        the "model exhausted output on thinking, content empty" failure mode
        that required a two-turn recovery hack in OllamaBackend.
      - reasoning_content returned as a separate field — content is always the
        actual response, never empty due to routing issues.

    Environment variables:
      SGLANG_BASE_URL         Server URL (default: http://localhost:30000/v1)
      SGLANG_MODEL_NAME       Served model name (default: MODEL env var)
      SGLANG_THINKING_BUDGET  Max reasoning tokens per request (default: 8192)
                              Set to 0 to disable thinking mode entirely.

    To use: set BACKEND=sglang in .env and start the server with build_sglang.sh.
    """

    def __init__(self):
        self._base_url = os.getenv("SGLANG_BASE_URL", "http://localhost:30000/v1")
        self._model_name = os.getenv("SGLANG_MODEL_NAME", MODEL)
        self._thinking_budget = int(os.getenv("SGLANG_THINKING_BUDGET", "8192"))

    def ask(self, prompt, system, model, max_tokens, think=None):
        from openai import OpenAI
        client = OpenAI(base_url=self._base_url, api_key="sglang", timeout=120)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Use the served model name from env, not the pipeline's internal model string.
        served_model = self._model_name

        # Look up think capability by the served model name, not the pipeline's
        # internal MODEL string (which is the Ollama name and would give wrong result).
        _think = _model_uses_think(served_model) if think is None else think

        extra_body = {}
        if _think and self._thinking_budget > 0:
            # SGLang exposes thinking_budget as an extra body parameter.
            # It caps reasoning tokens so the model always has budget left for
            # the actual response — eliminates empty-content responses entirely.
            extra_body["thinking_budget"] = self._thinking_budget

        chunks = []
        with client.chat.completions.create(  # type: ignore[union-attr]
            model=served_model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            extra_body=extra_body if extra_body else None,
            stream=True,
        ) as stream:
            for chunk in stream:
                delta = chunk.choices[0].delta
                # SGLang returns the actual response in .content, not .reasoning_content
                text = getattr(delta, "content", None)
                if text:
                    chunks.append(text)

        return "".join(chunks)


class VLLMBackend(LLMBackend):
    """
    vLLM server via its OpenAI-compatible /v1/chat/completions endpoint.

    vLLM advantages relevant to this pipeline:
      - Supports Qwen3 thinking via chat_template_kwargs (requires server started
        with --reasoning-parser qwen3 (vLLM 0.20+)).
      - Reasoning tokens go into delta.reasoning_content; actual response in
        delta.content — no empty-content surprises.
      - PagedAttention for efficient KV cache management across long contexts.

    Environment variables:
      VLLM_BASE_URL           Server URL (default: http://localhost:8000/v1)
      VLLM_MODEL_NAME         Served model name / HuggingFace repo ID
                              (default: MODEL env var, e.g. Qwen/Qwen3.6-27B-FP8)
      VLLM_THINKING_BUDGET    Max reasoning tokens per request (default: 8192)
                              Set to 0 to disable thinking mode entirely.

    To use: set BACKEND=vllm in .env and start the server, e.g.:
      vllm serve Qwen/Qwen3.6-27B-FP8 --reasoning-parser qwen3
    """

    def __init__(self):
        self._base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
        self._model_name = os.getenv("VLLM_MODEL_NAME", MODEL)
        # Budgets sized from the measured demand distribution (logs/thinking + llm.response
        # events): both thinking and content reach a censored p99.9 of ~13–16K tok, so each
        # is set to the next pow2 (16384). Total 32768 + input ≈ 28% of a 131072 ctx; the
        # empty/length followup below catches the residual tail. NOT derived from
        # OLLAMA_NUM_CTX (an Ollama-only knob that was throttling vLLM to 8192 content).
        self._thinking_budget = int(os.getenv("VLLM_THINKING_BUDGET", "16384"))
        self._content_budget = int(os.getenv("VLLM_CONTENT_BUDGET", "16384"))

    def ask(self, prompt, system, model, max_tokens, think=None):
        import re as _re

        from openai import BadRequestError, OpenAI
        global _vllm_cpt
        # connect=30: fail fast if vLLM is down.
        # read=900: thinking models can be silent for minutes mid-generation.
        client = OpenAI(base_url=self._base_url, api_key="vllm",
                        timeout=httpx.Timeout(connect=30, read=900, write=30, pool=30))

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        served_model = self._model_name

        # Look up think capability by served model name, not the pipeline's Ollama-named MODEL.
        _think = _model_uses_think(served_model) if think is None else think

        # vLLM controls per-request thinking via chat_template_kwargs.
        # Always send enable_thinking explicitly — Qwen3 defaults to thinking mode
        # if not told otherwise, and reasoning tokens go to reasoning_content (not
        # content), making the response appear empty to callers reading only content.
        _enable_thinking = _think and self._thinking_budget > 0
        _rep_penalty = float(os.getenv("VLLM_REPETITION_PENALTY", "1.05"))
        extra_body: dict = {
            "chat_template_kwargs": {"enable_thinking": _enable_thinking},
            "repetition_penalty": _rep_penalty,
        }

        _ctx = int(os.getenv("VLLM_MAX_MODEL_LEN", "65536"))
        _msg_chars = sum(len(m["content"]) for m in messages)
        _thinking_reserve = self._thinking_budget if _enable_thinking else 0
        # Content budget = the larger of what the caller asked for and the data-driven
        # floor. Raising the ceiling is safe: the mod gen_*/<FILE> early-stop detectors
        # terminate generation as soon as the block closes, so small responses still stop
        # early — only the large tail (p99 ~8K tok) benefits from the headroom.
        _content_cap = max(max_tokens, self._content_budget)

        def _cap(input_tok_est: int, content_budget: int) -> int:
            """Compute final max_tokens to send (caps content, then adds thinking bump)."""
            safe = max(512, _ctx - input_tok_est - _thinking_reserve - 512)
            capped = min(content_budget, safe)
            if _enable_thinking:
                _guard("vllm/max_tokens_thinking_bump",
                       f"model={served_model} content_budget={capped} total={capped + self._thinking_budget} "
                       f"(thinking_budget={self._thinking_budget}; ctx={_ctx} est_input={input_tok_est})")
                return capped + self._thinking_budget
            return capped

        # Initial estimate uses the adaptive chars-per-token ratio.
        # Floor 3.0: qwen3 on Rust/code measures 3.1–3.6 chars/token consistently.
        # The old floor of 2.0 overestimated input tokens for large prompts, causing
        # safe() to hit its 512 minimum and leaving almost no content output budget.
        _estimated_input_tokens = max(1, int(_msg_chars / max(3.0, _vllm_cpt)))
        _request_max_tokens = _cap(_estimated_input_tokens, _content_cap)

        chunks: list[str] = []
        thinking_chunks: list[str] = []
        _actual_prompt_tokens: int | None = None
        _finish_reason: str | None = None

        for _attempt in range(3):
            chunks = []
            thinking_chunks = []
            _actual_prompt_tokens = None
            _finish_reason = None
            _cbuf = ""
            _blk_tracking = False
            _blk_depth = 0
            _file_opens = 0
            _file_closes = 0
            _sock_open(f"vllm model={served_model} think={_enable_thinking} attempt={_attempt}")
            try:
                with client.chat.completions.create(  # type: ignore[call-overload,union-attr]
                    model=served_model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=_request_max_tokens,
                    extra_body=extra_body,
                    stream=True,
                    stream_options={"include_usage": True},
                ) as stream:
                    for chunk in stream:
                        # Usage arrives in a final chunk with empty choices — capture and skip.
                        if chunk.usage and chunk.usage.prompt_tokens:
                            _actual_prompt_tokens = chunk.usage.prompt_tokens
                        if not chunk.choices:
                            continue
                        # finish_reason=="length" ⇒ vLLM hit max_tokens (truncated);
                        # "stop" ⇒ natural EOS. Captured here so the post-loop recovery
                        # can tell a deliberate stop from a budget cut.
                        _fr = chunk.choices[0].finish_reason
                        if _fr:
                            _finish_reason = _fr
                        delta = chunk.choices[0].delta
                        # Collect reasoning tokens separately for logging/debugging.
                        reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                        if reasoning:
                            thinking_chunks.append(reasoning)
                            _sock_write(reasoning)
                        # Actual response is always in content.
                        text = getattr(delta, "content", None)
                        if not text:
                            continue
                        chunks.append(text)
                        _sock_write(text)
                        _cbuf += text
                        # Early-stop: mod gen_* block closed.
                        if not _blk_tracking:
                            _mg = _cbuf.find('mod gen_')
                            if _mg != -1:
                                _ob = _cbuf.find('{', _mg)
                                if _ob != -1:
                                    _blk_tracking = True
                                    for _ch in _cbuf[_ob:]:
                                        if _ch == '{': _blk_depth += 1
                                        elif _ch == '}': _blk_depth -= 1
                                    if _blk_depth <= 0:
                                        _sock_write("\n[BLOCK_CLOSED]\n")
                                        _guard("vllm/block_closed",
                                               f"model={served_model} mod gen_* closed at {len(_cbuf)} chars")
                                        break
                        else:
                            for _ch in text:
                                if _ch == '{': _blk_depth += 1
                                elif _ch == '}': _blk_depth -= 1
                            if _blk_depth <= 0:
                                _sock_write("\n[BLOCK_CLOSED]\n")
                                _guard("vllm/block_closed",
                                       f"model={served_model} mod gen_* closed at {len(_cbuf)} chars")
                                break
                        # Early-stop: all <FILE ...>...</FILE> blocks closed.
                        _file_opens  += text.count('<FILE ')
                        _file_closes += text.count('</FILE>')
                        if _file_opens > 0 and _file_opens == _file_closes:
                            _sock_write("\n[FILE_CLOSED]\n")
                            _guard("vllm/file_closed",
                                   f"model={served_model} {_file_opens} FILE block(s) closed at {len(_cbuf)} chars")
                            break
                break  # success — exit retry loop
            except BadRequestError as _e:
                _sock_close()
                # Parse the exact input token count vLLM reports in the 400 message:
                # "your prompt contains at least N input tokens"
                _m = _re.search(r'prompt contains at least (\d+) input tokens', str(_e))
                if _m and _attempt < 2:
                    _real_input = int(_m.group(1))
                    if _msg_chars > 0:
                        _measured_cpt = _msg_chars / _real_input
                        _prev_cpt = _vllm_cpt
                        _vllm_cpt = (1 - _vllm_cpt_alpha) * _vllm_cpt + _vllm_cpt_alpha * _measured_cpt
                        _guard("vllm/400_retry",
                               f"attempt={_attempt} real_input={_real_input}tok "
                               f"new_req_max={_cap(_real_input, _content_cap)} "
                               f"cpt_ewm={_prev_cpt:.2f}→{_vllm_cpt:.2f}")
                    _estimated_input_tokens = _real_input
                    _request_max_tokens = _cap(_real_input, _content_cap)
                    continue
                raise
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as _e:
                _sock_close()
                _guard("vllm/network_retry",
                       f"attempt={_attempt} err={type(_e).__name__}: {_e}")
                if _attempt < 2:
                    # Wait until vLLM is accepting requests again (up to 10 min).
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
                            break
                        except Exception:
                            time.sleep(5)
                    continue
                raise

        result = "".join(chunks)
        # Defensive: strip any <think>...</think> that leaked through content.
        result = _re.sub(r'<think>.*?</think>\s*', '', result, flags=_re.DOTALL).strip()

        # finish_reason=="length" means vLLM cut generation at max_tokens. (Early-stop
        # breaks abandon the stream before the final chunk, so _finish_reason stays None
        # on a clean block close — "length" therefore flags a genuine truncation.)
        _length_truncated = _finish_reason == "length"
        if _length_truncated:
            _guard("vllm/length_truncated",
                   f"model={served_model} hit max_tokens={_request_max_tokens} "
                   f"(content={len(result)} chars, thinking={sum(len(t) for t in thinking_chunks)} chars)")

        # Budget exhausted on reasoning: either no content emitted at all (all tokens →
        # reasoning_content) or the stream was length-truncated mid-output. Re-feed the
        # reasoning as an assistant turn and ask for code only, thinking disabled so it
        # doesn't re-reason. Mirrors the Ollama/compat followup paths.
        if thinking_chunks and (not result or _length_truncated):
            _thinking_text = "".join(thinking_chunks)
            # Keep the whole reasoning chain — thinking is disabled on the followup, so
            # length can't re-trigger reasoning, and vLLM's ctx (~131072) has ample room.
            # Only trim (from the front, preserving the conclusion) if it wouldn't leave
            # room for a usable code-output budget.
            _base_input = _actual_prompt_tokens or _estimated_input_tokens
            _cpt = max(3.0, _vllm_cpt)
            _output_reserve = self._content_budget
            _think_tok_budget = max(512, _ctx - _base_input - _output_reserve - 512)
            _think_char_budget = int(_think_tok_budget * _cpt)
            _thinking_tail = (_thinking_text[-_think_char_budget:]
                              if len(_thinking_text) > _think_char_budget else _thinking_text)
            _fu_messages = messages + [
                {"role": "assistant", "content": f"<think>\n{_thinking_tail}\n</think>"},
                {"role": "user", "content": (
                    "You have already reasoned through this problem above. "
                    "Now write the code block. Do not re-explain — output only the code."
                )},
            ]
            _fu_extra = {
                "chat_template_kwargs": {"enable_thinking": False},
                "repetition_penalty": _rep_penalty,
            }
            _fu_input_tok = _base_input + int(len(_thinking_tail) / _cpt) + 64
            _fu_max = min(_content_cap, max(512, _ctx - _fu_input_tok - 512))
            _dbg(f"FOLLOWUP vllm model={served_model} thinking_len={len(_thinking_text)} "
                 f"tail_len={len(_thinking_tail)} fu_max={_fu_max}")
            _fu_chunks: list[str] = []
            _fu_cbuf = ""
            _fu_blk_tracking = False
            _fu_blk_depth = 0
            _sock_open(f"vllm-followup model={served_model} think=False")
            _sock_write("\n[FOLLOWUP — thinking budget exhausted, requesting code]\n")
            try:
                with client.chat.completions.create(  # type: ignore[call-overload,union-attr]
                    model=served_model,
                    messages=_fu_messages,  # type: ignore[arg-type]
                    max_tokens=_fu_max,
                    extra_body=_fu_extra,
                    stream=True,
                ) as stream:
                    for chunk in stream:
                        if not chunk.choices:
                            continue
                        text = getattr(chunk.choices[0].delta, "content", None)
                        if not text:
                            continue
                        _fu_chunks.append(text)
                        _sock_write(text)
                        _fu_cbuf += text
                        if not _fu_blk_tracking:
                            _mg = _fu_cbuf.find('mod gen_')
                            if _mg != -1:
                                _ob = _fu_cbuf.find('{', _mg)
                                if _ob != -1:
                                    _fu_blk_tracking = True
                                    for _ch in _fu_cbuf[_ob:]:
                                        if _ch == '{': _fu_blk_depth += 1
                                        elif _ch == '}': _fu_blk_depth -= 1
                                    if _fu_blk_depth <= 0:
                                        _sock_write("\n[BLOCK_CLOSED]\n")
                                        break
                        else:
                            for _ch in text:
                                if _ch == '{': _fu_blk_depth += 1
                                elif _ch == '}': _fu_blk_depth -= 1
                            if _fu_blk_depth <= 0:
                                _sock_write("\n[BLOCK_CLOSED]\n")
                                break
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as _e:
                _guard("vllm/followup_network", f"err={type(_e).__name__}: {_e}")
            result = "".join(_fu_chunks)
            result = _re.sub(r'<think>.*?</think>\s*', '', result, flags=_re.DOTALL).strip()
            _guard("vllm/followup",
                   f"model={served_model} thinking_len={len(_thinking_text)} result_len={len(result)}")

        # Persist the model's reasoning (thinking) keyed by the response content
        # sha — the SAME sha the pipeline stores the response under — so callers
        # can read WHY the model produced what it did. Previously discarded; only
        # the content survived, so the reasoning behind a wrong fill was lost.
        if thinking_chunks:
            try:
                import hashlib as _hl
                _think_dir = os.path.join(_LOG_DIR, "thinking")
                os.makedirs(_think_dir, exist_ok=True)
                _sha = _hl.sha256(result.encode("utf-8", "replace")).hexdigest()
                _tp = os.path.join(_think_dir, f"{_sha}.txt")
                if not os.path.exists(_tp):
                    with open(_tp, "w", encoding="utf-8", errors="replace") as _tf:
                        _tf.write("".join(thinking_chunks))
            except Exception:
                pass

        # Update the adaptive chars-per-token ratio from actual usage stats.
        if _actual_prompt_tokens and _actual_prompt_tokens > 0 and _msg_chars > 0:
            _measured_cpt = _msg_chars / _actual_prompt_tokens
            _prev_cpt = _vllm_cpt
            _vllm_cpt = (1 - _vllm_cpt_alpha) * _vllm_cpt + _vllm_cpt_alpha * _measured_cpt
            _guard("vllm/cpt_update",
                   f"actual={_actual_prompt_tokens}tok est={_estimated_input_tokens}tok "
                   f"measured_cpt={_measured_cpt:.2f} ewm={_prev_cpt:.2f}→{_vllm_cpt:.2f}")

        _sock_close()
        if not result:
            _thinking_len = sum(len(t) for t in thinking_chunks)
            _dbg(f"EMPTY_RESPONSE vllm model={served_model} think={_enable_thinking} "
                 f"input_chars={sum(len(m.get('content') or '') for m in messages)} "
                 f"thinking_chars={_thinking_len}")
        return result


# ---------------------------------------------------------------------------
# Factory — selected once per process, reused for all ask() calls
# ---------------------------------------------------------------------------

def _backend_factory() -> LLMBackend:
    backend_env = os.getenv("BACKEND", "").lower()
    if backend_env == "huggingface":
        return HuggingFaceBackend()
    if backend_env == "sglang":
        return SGLangBackend()
    if backend_env == "vllm":
        return VLLMBackend()
    if backend_env == "claude" or MODEL.startswith("claude"):
        return ClaudeBackend()
    return OllamaBackend()


_backend: LLMBackend | None = None


def _get_backend() -> LLMBackend:
    global _backend
    if _backend is None:
        _backend = _backend_factory()
    return _backend


# ---------------------------------------------------------------------------
# Public API — unchanged signature; pipeline.py needs no edits
# ---------------------------------------------------------------------------

def ask(prompt: str, system: str | None = None, model: str | None = None,
        max_tokens: int = MAX_TOKENS, think: bool | None = None) -> str:
    if model is None:
        model = MODEL
    return _get_backend().ask(prompt, system, model, max_tokens, think=think)


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
