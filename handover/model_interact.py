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
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Model is read from .env (MODEL=...) or falls back to this default.
# For Ollama: MODEL=qwen2.5-coder:7b
# For Claude: MODEL=claude-haiku-4-5-20251001
# For HuggingFace: set BACKEND=huggingface and HF_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
MODEL = os.getenv("MODEL", "qwen2.5-coder:7b")

MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    @abstractmethod
    def ask(self, prompt: str, system: str | None, model: str, max_tokens: int) -> str: ...


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------

class ClaudeBackend(LLMBackend):
    def ask(self, prompt, system, model, max_tokens):
        import anthropic
        client = anthropic.Anthropic()
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message().content[0].text


class OllamaBackend(LLMBackend):
    def __init__(self):
        self._base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self._num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

    def ask(self, prompt, system, model, max_tokens):
        from openai import OpenAI
        client = OpenAI(base_url=self._base_url, api_key="ollama")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            extra_body={"options": {"num_ctx": self._num_ctx}},
        )
        return response.choices[0].message.content


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
              f"{set(str(d) for d in self._model.hf_device_map.values())}", file=sys.stderr)

    def ask(self, prompt, system, model, max_tokens):
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
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=min(max_tokens, self._max_new_tokens),
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        new_ids = output_ids[0][inputs["input_ids"].shape[1]:]
        return self._tokenizer.decode(new_ids, skip_special_tokens=True)


# ---------------------------------------------------------------------------
# Factory — selected once per process, reused for all ask() calls
# ---------------------------------------------------------------------------

def _backend_factory() -> LLMBackend:
    backend_env = os.getenv("BACKEND", "").lower()
    if backend_env == "huggingface":
        return HuggingFaceBackend()
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
        max_tokens: int = MAX_TOKENS) -> str:
    if model is None:
        model = MODEL
    return _get_backend().ask(prompt, system, model, max_tokens)


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
        with open(args.file, "r") as f:
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
