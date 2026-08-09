"""Unit coverage for the testable surface of llm_transports (config, sock infra,
factory, base defaults). The transport stream() bodies do live network/model I/O
and are validated by microcrate/anyhow runs, not units."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pipeline_core.adapters import llm_transports as t


def test_model_cap_helpers_default_and_lookup() -> None:
    assert t._model_uses_think("qwen3:32b") is True
    # `qwen3.6:27b-q8` was pinned think=False from 2026-05-16 to 2026-08-09 and
    # was never once True, while the stated intent for it was always True. The
    # pin worked around `_stream_compat` reading only `delta.content`: Qwen3.6
    # puts reasoning in a separate field, so content stayed empty for the whole
    # reasoning phase. That path reads both field names now.
    assert t._model_uses_think("qwen3.6:27b-q8") is True
    assert t._model_uses_think("unknown-model") is True          # default think
    assert t._model_use_native_ollama("qwen3.6:27b") is True
    assert t._model_use_native_ollama("qwen3.6:27b-q8") is False  # compat path
    assert t._model_use_native_ollama("unknown-model") is False  # default


def test_the_compat_stream_reads_a_separate_reasoning_field() -> None:
    """The 139 backport. `_stream_compat` used to read only
    `delta.content` and run `InlineThinkSplitter` over it, which handles Qwen3's
    inline `<think>` tags. Qwen3.6 emits reasoning in a separate field, so on
    this path content stayed empty through the whole reasoning phase, the
    splitter found no tags, and the response came back blank — the reason for
    the think=False pin above."""
    import inspect
    src = inspect.getsource(t.OllamaTransport._stream_compat)
    assert '"reasoning"' in src and '"reasoning_content"' in src, \
        "the dual-field read is gone; qwen3.6 on the compat path returns empty"


def test_default_max_tokens_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TOKENS", "1234")
    assert t._default_max_tokens() == 1234


def test_default_max_tokens_from_ollama_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_TOKENS", raising=False)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "20000")
    assert t._default_max_tokens() == 20000 - 8192


def test_default_max_tokens_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("MAX_TOKENS", "OLLAMA_NUM_CTX", "VLLM_MAX_MODEL_LEN"):
        monkeypatch.delenv(v, raising=False)
    assert t._default_max_tokens() == 32768


def test_parse_input_token_overflow_extracts_and_rejects() -> None:
    assert t._parse_input_token_overflow(
        "... prompt contains at least 41234 input tokens ...") == 41234
    assert t._parse_input_token_overflow("unrelated 400 error") is None
    assert t._parse_input_token_overflow("prompt contains at least 12 banana") is None


def test_backend_factory_selects_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND", "vllm")
    assert type(t._backend_factory()).__name__ == "VLLMTransport"
    monkeypatch.setenv("BACKEND", "sglang")
    assert type(t._backend_factory()).__name__ == "SGLangTransport"
    monkeypatch.setenv("BACKEND", "")
    assert type(t._backend_factory()).__name__ == "OllamaTransport"


def test_base_transport_defaults() -> None:
    bt = t._BaseTransport()
    assert bt.resolve_thinking("m", None) is False
    assert bt.followup_messages([{"role": "user"}], "x", model="m") == [{"role": "user"}]
    assert bt.clean("hello", model="m") == "hello"
    assert bt.wait_until_ready() is None
    assert bt.budget_state() is bt._state


def test_stream_path_main_vs_worker() -> None:
    assert t._stream_path_for_thread() == t._STREAM_LOG  # main thread
    out: dict[str, str] = {}

    def _w() -> None:
        out["p"] = t._stream_path_for_thread()

    th = threading.Thread(target=_w, name="per-fn-w0")
    th.start()
    th.join()
    assert out["p"] != t._STREAM_LOG and "per-fn-w0" in out["p"]


def test_sock_open_write_close_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "stream.log"
    monkeypatch.setattr(t, "_STREAM_LOG", str(p))
    t._sock_open(label="call-x")
    t._sock_write("<tok>")
    t._sock_close()
    body = p.read_text()
    assert "CALL: call-x" in body and "<tok>" in body


def test_timed_iter_passes_through_and_raises_on_error() -> None:
    assert list(t._timed_iter(iter([1, 2, 3]), timeout=5)) == [1, 2, 3]

    def _boom() -> object:
        yield 1
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        list(t._timed_iter(_boom(), timeout=5))
