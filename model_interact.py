#!/usr/bin/env python3
"""
Send a prompt to a language model and return the response.

Backend is selected via the BACKEND environment variable (or .env file):
  - BACKEND=huggingface                                     → HuggingFace transformers
  - BACKEND=vllm / sglang                                   → vLLM / SGLang OpenAI API
  - BACKEND=ollama        (default)                         → Ollama local API

This pipeline is self-hosted only (Hard Rule 7): no external/cloud LLM backend.

The transports themselves live in `pipeline_core.adapters.llm_transports`,
relocated out of pipeline.py; this module is the composition root (build backend + controller, run
`ask`) and the CLI. It re-exports the transport-layer names that external callers
and tests already import from `model_interact`.

Usage:
  python model_interact.py "your prompt here"
  python model_interact.py --file prompt.txt
  echo "prompt" | python model_interact.py
  python model_interact.py "prompt" --output response.txt
  python model_interact.py "prompt" --system "system prompt"
  python model_interact.py "prompt" --model qwen2.5-coder:7b
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from pipeline_core.llm import LLMController, Transport

# Transports read MODEL/MAX_TOKENS from the environment at import time, so .env
# must be loaded BEFORE that module is imported.
load_dotenv(Path(__file__).parent / ".env")

from pipeline_core.adapters.llm_transports import (  # noqa: E402  (after load_dotenv)
    MAX_TOKENS,
    MODEL,
    _backend_factory,
    _make_hooks,
    _parse_input_token_overflow,
    _sock_close,
    _sock_open,
    _sock_write,
    _stream_path_for_thread,
)

# Re-exported transport-layer surface (imported as `model_interact.X` by
# pipeline.py and the transport/parallelism tests).
__all__ = [
    "MODEL",
    "MAX_TOKENS",
    "ask",
    "ask_turns",
    "main",
    "_parse_input_token_overflow",
    "_sock_open",
    "_sock_write",
    "_sock_close",
    "_stream_path_for_thread",
]


# ---------------------------------------------------------------------------
# Composition — backend + controller selected once per process, reused per ask().
# ---------------------------------------------------------------------------

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
        max_tokens: int = MAX_TOKENS, think: bool | None = None,
        temperature: float | None = None, followup_think: bool = False) -> str:
    if model is None:
        model = MODEL
    return _get_controller().run(
        _get_backend(), system=system, prompt=prompt, model=model,
        max_tokens=max_tokens, want_think=think, temperature=temperature, followup_think=followup_think,
    )


def ask_turns(messages: list[dict[str, Any]], model: str | None = None,
              max_tokens: int = MAX_TOKENS, think: bool | None = None,
              temperature: float | None = None, followup_think: bool = False) -> str:
    """One call over a caller-built transcript (`LLMController.run_turns`)."""
    if model is None:
        model = MODEL
    return _get_controller().run_turns(
        _get_backend(), messages=messages, model=model, max_tokens=max_tokens,
        want_think=think, temperature=temperature, followup_think=followup_think,
    )


def main() -> None:
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
