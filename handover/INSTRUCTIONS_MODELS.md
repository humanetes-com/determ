# Model Configuration

The pipeline supports two model providers: Ollama (local, free) and Claude (API, paid).
The active model is controlled by a single line in your .env file.

---

## Option 1 -- Ollama (local, free)

Ollama runs open-source models on your own hardware. No API key, no cost per token.
Requires a machine with a decent GPU (8GB VRAM minimum recommended).

### Step 1 -- Install Ollama

Download and install from https://ollama.com

On Linux:

    curl -fsSL https://ollama.com/install.sh | sh

On macOS: download the app from the website.

### Step 2 -- Pull a model

For 8GB VRAM:

    ollama pull qwen2.5-coder:7b

For 20GB+ VRAM (much more capable):

    ollama pull qwen2.5-coder:32b

For 40GB+ VRAM (best local quality):

    ollama pull qwen3:72b

### Step 3 -- Verify Ollama is running

Ollama starts automatically after install. To check:

    ollama list

You should see the model you pulled listed.

### Step 4 -- Configure .env

In your .env file set:

    MODEL=qwen2.5-coder:7b

No API key needed. The pipeline will connect to Ollama at http://localhost:11434.

If Ollama runs on a different machine on your network, also set:

    OLLAMA_BASE_URL=http://<machine-ip>:11434/v1

---

## Option 2 -- Claude (API, paid)

Claude is Anthropic's hosted model. It is more capable than local 7B models,
especially for complex code generation and reasoning, but charges per token used.

Costs (approximate, per million tokens):
  claude-haiku-4-5-20251001   $0.80 input  /  $4 output   (recommended, cheapest)
  claude-sonnet-4-6           $3 input     /  $15 output
  claude-opus-4-7             $15 input    /  $75 output   (most capable, most expensive)

For this experiment, Haiku is the recommended starting point. Opus is powerful but
a single full run on a large project like agave can cost $5-10+.

### Step 1 -- Create an Anthropic account

Go to https://console.anthropic.com and sign up.

Note: a Claude.ai subscription (claude.ai) does NOT include API access.
The API is billed separately through console.anthropic.com.

### Step 2 -- Add credits

Go to Plans & Billing on console.anthropic.com and add a credit card.
Purchase at least $5 of credits to get started.

### Step 3 -- Create an API key

Go to Settings -> API Keys -> Create Key.
Copy the key -- it is only shown once.

### Step 4 -- Configure .env

In your .env file set:

    MODEL=claude-haiku-4-5-20251001
    ANTHROPIC_API_KEY=sk-ant-...your key here...

To use a more capable model, change MODEL:

    MODEL=claude-opus-4-7

---

## Switching between providers

Edit one line in .env and restart the pipeline. No code changes needed.

    # Use local Ollama
    MODEL=qwen2.5-coder:7b

    # Use Claude Haiku (cheap)
    MODEL=claude-haiku-4-5-20251001

    # Use Claude Opus (most capable)
    MODEL=claude-opus-4-7

---

## Which should I use?

    Local hardware with 8GB+ VRAM   -> start with Ollama, it is free
    No GPU / low VRAM               -> use Claude Haiku
    Best possible results           -> use Claude Opus (monitor costs closely)

For developing and testing the methodology on small projects, Ollama is sufficient.
For the final run on a large project like agave, Claude Opus will produce better results.

---

## Model Benchmark Comparison (April 2026)

This section compares the best open-weight models available locally via Ollama,
ranked by coding and requirements-writing quality. Updated April 2026.

### VRAM tiers (Q4_K_M quantization)

| Config          | Total VRAM | Best reachable model           |
|-----------------|------------|--------------------------------|
| Single 24GB GPU | 24GB       | Qwen2.5-Coder 32B / Qwen3 32B  |
| 24GB + 8GB      | 32GB       | Qwen3 32B / GLM-4.7            |
| 24GB + 16GB     | 40GB       | **Qwen3 72B** (recommended)    |
| 24GB + 24GB     | 48GB       | Qwen3 72B Q8, higher quality   |

---

### Top 20 -- Coding

| #  | Model                | VRAM Q4 | 32GB | 40GB | 48GB | Notes                                 |
|----|----------------------|---------|------|------|------|---------------------------------------|
|  1 | Qwen3 72B            | ~40GB   | ✗    | ✓    | ✓    | Best coding+reasoning 2026            |
|  2 | Qwen2.5-Coder 72B    | ~40GB   | ✗    | ✓    | ✓    | Code-specialized, excellent           |
|  3 | DeepSeek-R1 70B      | ~40GB   | ✗    | ✓    | ✓    | Strong reasoning for code             |
|  4 | Llama 3.3 70B        | ~40GB   | ✗    | ✓    | ✓    | Solid general + code                  |
|  5 | GLM-4.7              | ~20GB   | ✓    | ✓    | ✓    | 89% LiveCodeBench, top score          |
|  6 | Qwen3 35B-A3B (MoE)  | ~22GB   | ✓    | ✓    | ✓    | 73% SWE-bench, fits 24GB single       |
|  7 | Qwen3 32B            | ~20GB   | ✓    | ✓    | ✓    | Best dense 32B available              |
|  8 | Qwen2.5-Coder 32B    | ~20GB   | ✓    | ✓    | ✓    | Proven, strong baseline               |
|  9 | DeepSeek-Coder V2 16B| ~10GB   | ✓    | ✓    | ✓    | Fast, good quality                    |
| 10 | Gemma 4 27B          | ~17GB   | ✓    | ✓    | ✓    | Google, strong code                   |
| 11 | MiMo-V2-Flash        | ~10GB   | ✓    | ✓    | ✓    | Beats DeepSeek V3.2 on SWE-bench      |
| 12 | Qwen3 14B            | ~9GB    | ✓    | ✓    | ✓    | 81.1 MMLU, punches above weight       |
| 13 | Llama 4 Scout 17B    | ~11GB   | ✓    | ✓    | ✓    | Meta, long context                    |
| 14 | DeepSeek-R1 32B      | ~20GB   | ✓    | ✓    | ✓    | Reasoning distill, good code          |
| 15 | Phi-4 14B            | ~9GB    | ✓    | ✓    | ✓    | Microsoft, efficient                  |
| 16 | Qwen3 8B             | ~5GB    | ✓    | ✓    | ✓    | Best small model for code             |
| 17 | Mistral Small 3.1 24B| ~15GB   | ✓    | ✓    | ✓    | Fast, strong instruction following    |
| 18 | CodeLlama 34B        | ~21GB   | ✓    | ✓    | ✓    | Older but battle-tested               |
| 19 | StarCoder2 15B       | ~10GB   | ✓    | ✓    | ✓    | Code-only, HuggingFace                |
| 20 | Qwen2.5-Coder 7B     | ~5GB    | ✓    | ✓    | ✓    | Lightweight fallback                  |

---

### Top 20 -- Functional Requirements Writing

| #  | Model                | VRAM Q4 | 32GB | 40GB | 48GB | Notes                                 |
|----|----------------------|---------|------|------|------|---------------------------------------|
|  1 | Qwen3 72B            | ~40GB   | ✗    | ✓    | ✓    | Best reasoning + structured writing   |
|  2 | Llama 3.3 70B        | ~40GB   | ✗    | ✓    | ✓    | Excellent instruction following       |
|  3 | DeepSeek-R1 70B      | ~40GB   | ✗    | ✓    | ✓    | Deep reasoning, precise specs         |
|  4 | Qwen3 32B            | ~20GB   | ✓    | ✓    | ✓    | Strong analytical writing             |
|  5 | GLM-5.1              | ~20GB   | ✓    | ✓    | ✓    | 58% SWE-bench Pro, structured output  |
|  6 | Mistral Large 2      | ~24GB   | ✓    | ✓    | ✓    | Best instruction compliance           |
|  7 | Gemma 4 27B          | ~17GB   | ✓    | ✓    | ✓    | Google, precise outputs               |
|  8 | Qwen3 35B-A3B        | ~22GB   | ✓    | ✓    | ✓    | Good analytical depth                 |
|  9 | Llama 4 Scout 17B    | ~11GB   | ✓    | ✓    | ✓    | Long context, good for documents      |
| 10 | DeepSeek-R1 32B      | ~20GB   | ✓    | ✓    | ✓    | Reasoning distill                     |
| 11 | Qwen2.5 72B          | ~40GB   | ✗    | ✓    | ✓    | General purpose, very capable         |
| 12 | Mistral Small 3.1 24B| ~15GB   | ✓    | ✓    | ✓    | Fast, obedient                        |
| 13 | Phi-4 14B            | ~9GB    | ✓    | ✓    | ✓    | Surprisingly good at analysis         |
| 14 | Qwen3 14B            | ~9GB    | ✓    | ✓    | ✓    | 81.1 MMLU                             |
| 15 | Llama 3.1 8B         | ~5GB    | ✓    | ✓    | ✓    | Lightweight baseline                  |
| 16 | Gemma 4 9B           | ~6GB    | ✓    | ✓    | ✓    | Compact, good writing                 |
| 17 | Qwen3 8B             | ~5GB    | ✓    | ✓    | ✓    | Good reasoning at 8B                  |
| 18 | Mistral 7B v0.3      | ~5GB    | ✓    | ✓    | ✓    | Proven, minimal VRAM                  |
| 19 | Phi-3 Mini 3.8B      | ~3GB    | ✓    | ✓    | ✓    | Emergency fallback                    |
| 20 | Command R+ 104B      | ~60GB   | ✗    | ✗    | ✗    | Needs 3x GPU, listed for reference    |

---

### Recommendation for this pipeline

The pipeline does two things: Rust code synthesis (Task 1/2) and requirements analysis.
One model handles both. Recommended choices by hardware:

- **32GB (24+8):**  `qwen3:32b` or `glm4.7` -- best available without GPU upgrade
- **40GB (24+16):** `qwen3:72b` -- tops both lists, handles code and requirements
- **48GB (24+24):** `qwen3:72b` at higher quantization (Q6/Q8) for better quality

To run on 40GB+ multi-GPU with Ollama:

    ollama pull qwen3:72b
    # Ollama auto-splits across GPUs via tensor parallelism

---

### Sources

- [Best Local Coding Models Ranked: Every VRAM Tier 2026](https://insiderllm.com/guides/best-local-coding-models-2026/)
- [Best Open Source LLM 2026 Ranking + Ollama Guide](https://whatllm.org/best-open-source-llm)
- [DeepSeek V3.2 vs Llama 4 vs Qwen3: Best Open-Source LLM for Production 2026](https://www.spheron.network/blog/deepseek-vs-llama-4-vs-qwen3/)
- [Open Source LLM Leaderboard 2026](https://www.vellum.ai/open-llm-leaderboard)
- [Best Open-Source LLMs April 2026](https://lushbinary.com/blog/best-open-source-llms-april-2026-comparison-guide/)
- [Local LLM Hardware Guide 2026](https://www.promptquorum.com/local-llms/local-llm-hardware-guide-2026/)
- [Home GPU LLM Leaderboard by VRAM Tier](https://awesomeagents.ai/leaderboards/home-gpu-llm-leaderboard/)
- [LLM Coding Benchmark Showdown 2026](https://tolearn.blog/blog/llm-coding-benchmark-comparison-2026)
- [Best LLM for Coding 2026](https://whatllm.org/best-llm-for-coding)
