"""Every transport fills the token counts a phase's cost is measured in.

`StreamMeta.prompt_tokens` used to be filled by vLLM alone and no transport
read completion tokens, so a phase's tokens read zero on every other backend.
Each stream body is driven here against a fake client module: the OpenAI
protocol sends usage on a final chunk with NO choices (which the SGLang loop
used to index blindly), Ollama's native API on its `done` part."""
from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace as NS
from typing import Any

import pytest

from pipeline_core.adapters import llm_transports as t
from pipeline_core.domain.model_profile import EffortLevel
from pipeline_core.llm.controller import Hooks, LLMController
from pipeline_core.llm.events import StreamMeta
from tests.test_llm_controller import SENTINEL, FakeTransport


def _chunk(text: str | None = None, usage: Any = None) -> NS:
    choices = [] if text is None else [NS(delta=NS(content=text, reasoning=None,
                                                    reasoning_content=None),
                                          finish_reason="stop")]
    return NS(choices=choices, usage=usage)


def _fake_openai(chunks: list[NS], seen: dict[str, Any]) -> types.ModuleType:
    mod = types.ModuleType("openai")

    class _Err(Exception):
        pass

    class OpenAI:
        def __init__(self, **_kw: Any) -> None:
            self.chat = NS(completions=NS(create=self._create))

        @staticmethod
        @contextmanager
        def _create(**kw: Any):
            seen.update(kw)
            yield iter(chunks)

    mod.OpenAI = OpenAI  # type: ignore[attr-defined]
    mod.APIConnectionError = _Err  # type: ignore[attr-defined]
    mod.BadRequestError = _Err  # type: ignore[attr-defined]
    mod.Timeout = lambda **_kw: None  # type: ignore[attr-defined]
    return mod


_CHUNKS = [_chunk("fn a() {}"), _chunk(None, NS(prompt_tokens=900, completion_tokens=37))]


@pytest.mark.parametrize("make", [
    lambda: t.VLLMTransport(),
    lambda: t.SGLangTransport(),
    lambda: t.OllamaTransport(),
], ids=["vllm", "sglang", "ollama-compat"])
def test_openai_protocol_transports_read_usage(monkeypatch, make) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(_CHUNKS, seen))
    tr = make()
    meta = StreamMeta()
    out = list(tr.stream([{"role": "user", "content": "x"}], model="qwen3.6:27b-q8",
                         max_tokens=10, enable_thinking=False, meta=meta,
                         effort=EffortLevel.MAX))
    assert "".join(d.content for d in out) == "fn a() {}"
    assert (meta.prompt_tokens, meta.completion_tokens) == (900, 37)
    assert seen["stream_options"] == {"include_usage": True}


def test_ollama_native_reads_the_done_part(monkeypatch) -> None:
    parts = [NS(message=NS(content="ok", thinking=None), done=False),
             NS(message=NS(content="", thinking=None), done=True,
                prompt_eval_count=512, eval_count=64)]
    mod = types.ModuleType("ollama")
    mod.chat = lambda **_kw: iter(parts)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ollama", mod)
    meta = StreamMeta()
    list(t.OllamaTransport().stream([{"role": "user", "content": "x"}],
                                    model="qwen3.6:27b", max_tokens=None,
                                    enable_thinking=False, meta=meta,
                                    effort=EffortLevel.MAX))
    assert (meta.prompt_tokens, meta.completion_tokens) == (512, 64)


def test_a_server_that_sends_no_usage_leaves_the_counts_unset(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", _fake_openai([_chunk("x")], {}))
    meta = StreamMeta()
    list(t.VLLMTransport().stream([{"role": "user", "content": "x"}], model="m",
                                  max_tokens=10, enable_thinking=False, meta=meta,
                         effort=EffortLevel.MAX))
    assert (meta.prompt_tokens, meta.completion_tokens) == (None, None)


class _CountingTransport(FakeTransport):
    def stream(self, messages, *, model, max_tokens, enable_thinking, meta,
               effort=None, temperature=None):
        yield from super().stream(messages, model=model, max_tokens=max_tokens,
                                  enable_thinking=enable_thinking, meta=meta,
                                  effort=effort, temperature=temperature)
        meta.completion_tokens = 41


def test_stream_meta_event_carries_completion_tokens() -> None:
    from pipeline_core.llm import Delta
    events: list[dict[str, Any]] = []
    ctl = LLMController(Hooks(emit=lambda name, data: events.append({"event": name, **data})),
                        default_sentinel=SENTINEL)
    ctl.run(_CountingTransport([[Delta(content="fn f() {}")]], prompt_tokens=[700]),
            system="s", prompt="p", model="m", max_tokens=SENTINEL)
    [meta] = [e for e in events if e["event"] == "llm.stream_meta"]
    assert (meta["prompt_tokens"], meta["completion_tokens"]) == (700, 41)
