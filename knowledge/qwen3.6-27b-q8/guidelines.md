# Qwen3.6-27B Q8_0 — Operational Guidelines

Sources: HuggingFace model card, Ollama library, Unsloth docs, InsiderLLM, r/LocalLLaMA (researched 2026-05-11).

---

## Model Identity

- **Official name:** Qwen3.6-27B (Alibaba Qwen team, 2026)
- **Architecture:** Dense hybrid — gated DeltaNet (linear attention) + gated attention. NOT MoE.
- **Parameters:** 27B
- **Native context:** 262,144 tokens; extendable to ~1M via YaRN rope scaling
- **License:** Apache 2.0

Our deployment: Q8_0 GGUF (~28.6 GB, ~8 bpp, near-lossless), loaded into Ollama via `ModelfileHf`, served as `qwen3.6:27b-q8`.

---

## Thinking Mode

Thinking mode is **on by default**. The model reasons inside `<think>…</think>` tags before producing output.

### Recommended sampling for coding + thinking

| Parameter      | Value  | Notes                                             |
|----------------|--------|---------------------------------------------------|
| temperature    | 0.6    | Low = less variance per retry; 1.0 default is too high for syntax-critical codegen |
| top_p          | 0.95   | Official Qwen recommendation                      |
| top_k          | 20     | Official recommendation for thinking mode; Ollama default is 40 |
| min_p          | 0.05   | Filters very unlikely tokens, keeps Rust syntax clean |
| presence_penalty | 0.0  | 0.0 for thinking mode; 1.5 only for non-thinking instruct mode |
| max_tokens     | ~155K (auto) | **Critical**: thinking tokens count against this limit. 4096 (old default) is exhausted by reasoning alone. Now derived as `OLLAMA_NUM_CTX - 8192`; override with `MAX_TOKENS` in `.env`. |

### Disabling thinking (non-thinking instruct mode)
Not used in this pipeline. If ever needed via vLLM/SGLang:
```python
extra_body={"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}
```
Raise `presence_penalty` to 1.5 when thinking is off.

### Thinking preservation (multi-turn agents)
For agentic workflows: pass the model's `<think>` block back as an assistant turn so it continues without re-reasoning from scratch. This is what the native ollama path and the compat-path follow-up both implement.

Via SGLang/vLLM native API:
```python
extra_body={"chat_template_kwargs": {"preserve_thinking": True}, "top_k": 20}
```

---

## Known Failure Modes

### Empty content after thinking
**Symptom:** Model generates a full `<think>` block but produces no code after `</think>`. Content field is empty.

**Cause:** Model exhausts its output budget on reasoning and produces nothing. Also triggered by stale/mismatched Jinja chat templates in some GGUF builds.

**Fix in this pipeline:** `model_interact.py` detects empty post-strip content, sends a follow-up with the thinking embedded as plain assistant content, asks the model to write the code without re-reasoning. See `OllamaBackend.ask()`.

**Fix for llama.cpp/vLLM backends:** Pull the latest Jinja template from the official model card and pass it explicitly:
```bash
llama-server --jinja --chat-template-file qwen3.6.jinja
```

### Gibberish output on CUDA 13.2
Multiple community reports of Q4–Q8 GGUF producing gibberish. Pin CUDA 13.1 or use a newer llama.cpp build.

### Ollama direct pull incompatibility
`ollama pull qwen3.6:27b` gives Q4_K_M (17 GB). The community GGUF from bartowski/unsloth does not load directly into Ollama due to multimodal projection (mmproj) files bundled in the repo. Our `ModelfileHf` works around this by pointing directly at the text-only GGUF file, bypassing the mmproj issue entirely.

---

## Context Window

- **Native:** 262,144 tokens
- **Minimum for coherent thinking:** 128K tokens (drop below this and reasoning degrades)
- **Our deployment:** 163,840 tokens (`OLLAMA_NUM_CTX` in `.env`), overrides the Modelfile default at request time via `extra_body options`
- **KV cache:** only 16/64 layers use KV cache (SSM layers don't) — memory per token is much lower than a full-attention model of the same size

---

## Hardware (dual RTX 3090, 48 GB VRAM)

| Component              | Memory   |
|------------------------|----------|
| Q8_0 weights           | ~28.6 GB |
| KV cache (163K ctx, q8_0) | ~5.1 GB |
| Runtime overhead       | ~1.5 GB  |
| **Total**              | **~35 GB** |
| Headroom               | ~13 GB   |

KV cache type is set to `q8_0` via `OLLAMA_KV_CACHE_TYPE=q8_0` in `.env`.

---

## Benchmarks (thinking mode, official)

| Benchmark          | Qwen3.6-27B | Qwen3.5-397B-A17B | Notes           |
|--------------------|-------------|-------------------|-----------------|
| SWE-bench Verified | 77.2%       | 76.2%             | 27B beats 397B  |
| Terminal-Bench     | 59.3%       | 52.5%             |                 |
| SkillsBench        | 48.2%       | 30.0%             | +18 points      |

Community: Q4_K_M hits ~25 tok/s on a single 3090; Q8_0 on dual 3090s runs comfortably within VRAM.

---

## GGUF Quantization Reference

| Quantization | Size    | Quality      |
|--------------|---------|--------------|
| IQ4_NL       | 16.1 GB | Compressed   |
| Q4_K_M       | 17.5 GB | Balanced (Ollama default pull) |
| Q6_K         | 23.2 GB | High         |
| Q8_0         | 28.7 GB | Near-lossless ← **our choice** |
| BF16         | 55 GB   | Full precision |

Q8_0 chosen for syntax-critical Rust codegen: fewer hallucinated API calls and malformed syntax than Q4_K_M.

---

## Ollama Modelfile Notes

- `TEMPLATE` is inherited from the GGUF — do not override unless the base template is broken.
- `SYSTEM` is intentionally omitted — pipeline sends task-specific system prompts per call.
- `stop "```"` is intentionally omitted — would halt generation before any fenced code block.
- `repeat_last_n 1024` prevents boilerplate loops in long generated code blocks.
