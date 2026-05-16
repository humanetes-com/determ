#!/usr/bin/env python3
"""Probe the OpenAI-compat response to see what fields thinking tokens arrive in."""
import os, sys
os.environ.setdefault("OLLAMA_NUM_CTX", "8192")
sys.path.insert(0, "/home/user/determ")

from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama", timeout=60)

print("Sending short think=True prompt via compat layer...\n")
fields_seen = set()
content_chunks = []
reasoning_chunks = []
raw_extras = []

with client.chat.completions.create(
    model="qwen3.6:27b-q8",
    messages=[{"role": "user", "content": "What is 2+2? Think step by step."}],
    max_tokens=512,
    extra_body={"options": {"num_ctx": 8192}},
    stream=True,
) as stream:
    for chunk in stream:
        delta = chunk.choices[0].delta
        fields_seen.update(delta.model_fields_set)

        content = delta.content
        if content:
            content_chunks.append(content)

        # Check for reasoning_content via model_extra
        extra = getattr(delta, "model_extra", {}) or {}
        reasoning = extra.get("reasoning_content") or getattr(delta, "reasoning_content", None)
        if reasoning:
            reasoning_chunks.append(reasoning)
            if reasoning not in raw_extras:
                raw_extras.append(reasoning[:80])

print(f"Delta fields seen:       {fields_seen}")
print(f"model_extra keys (last): {list(getattr(chunk.choices[0].delta, 'model_extra', {}).keys())}")
print(f"content chunks:          {len(content_chunks)}  ({sum(len(c) for c in content_chunks)} chars)")
print(f"reasoning chunks:        {len(reasoning_chunks)}  ({sum(len(r) for r in reasoning_chunks)} chars)")
print(f"\ncontent result:  {(''.join(content_chunks))[:200]!r}")
print(f"reasoning sample: {raw_extras[:3]}")
