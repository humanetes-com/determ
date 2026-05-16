# HuggingFace Local Setup

---

## 1. Python environment

### Option A — use the project's existing venv (recommended if already set up)

```bash
source /home/user/determ/.venv/bin/activate
```

### Option B — create a new venv (if starting fresh or on a new server)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Option C — skip venv entirely (system Python, conda, or container)

If you are already inside a conda environment, a Docker container, or a managed
environment, skip venv creation and go straight to step 2.

**How to tell which situation you are in:**

```bash
# Are you already in a venv or conda env?
echo $VIRTUAL_ENV    # non-empty = venv active
echo $CONDA_DEFAULT_ENV  # non-empty = conda active

# What Python is active?
which python3
```

If either variable is non-empty, skip venv creation — just install into the
active environment.

---

## 2. Install dependencies

```bash
pip install transformers accelerate
pip install bitsandbytes   # only needed for 4-bit / 8-bit quantization
pip install huggingface_hub  # only needed for model search and auth
```

> **Note:** `bitsandbytes` requires a CUDA-capable GPU and matching CUDA toolkit.
> If you hit a build error, check your CUDA version:
> ```bash
> nvcc --version
> nvidia-smi
> ```
> Then install the matching bitsandbytes wheel:
> ```bash
> pip install bitsandbytes --extra-index-url https://huggingface.github.io/bitsandbytes-wheels/
> ```

---

## 3. Authenticate (gated models only)

**Not required for Qwen or DeepSeek models — skip unless you need Llama.**

```bash
huggingface-cli login   # paste your token from hf.co/settings/tokens
```

Verify:
```bash
huggingface-cli whoami
```

---

## 4. Find the best current model

Benchmarks move fast. Before picking a model, check what is best right now:

```bash
# Search code models by downloads
python3 - <<'EOF'
from huggingface_hub import list_models

models = list_models(
    task="text-generation",
    tags=["code"],
    sort="downloads",
    direction=-1,
    limit=20,
)
for m in models:
    print(f"{m.id:60s}  downloads: {m.downloads}")
EOF
```

Then check the live leaderboards for benchmark scores:
- **BigCodeBench**: https://huggingface.co/spaces/bigcode/bigcodebench-leaderboard
- **EvalPlus / HumanEval**: https://evalplus.github.io/leaderboard.html
- **LiveCodeBench**: https://livecodebench.github.io/leaderboard.html

---

## 5. Check VRAM before downloading

Estimate memory requirements without downloading the full model weights:

```bash
python3 - <<'EOF'
from huggingface_hub import get_safetensors_metadata

model_id = "Qwen/Qwen2.5-Coder-32B-Instruct"   # change this
meta = get_safetensors_metadata(model_id)
total = sum(meta.parameter_count.values())
print(f"Model:           {model_id}")
print(f"Parameters:      {total / 1e9:.1f}B")
print(f"bfloat16 VRAM:   {total * 2 / 1e9:.1f} GB")
print(f"8-bit VRAM:      {total * 1 / 1e9:.1f} GB")
print(f"4-bit VRAM:      {total * 0.5 / 1e9:.1f} GB  (+ ~2-4 GB overhead)")
EOF
```

**Your available VRAM:** 24 GB + 8 GB = 32 GB total

| Model | 4-bit VRAM | Fits? | Uses both GPUs? |
|---|---|---|---|
| `Qwen/Qwen2.5-Coder-32B-Instruct` | ~18 GB | Yes | No — 24 GB card only |
| `Qwen/Qwen2.5-72B-Instruct` | ~40 GB | Borderline OOM | Yes but risky |
| `mistralai/Codestral-22B-v0.1` | ~11 GB | Yes | No — 24 GB card only |
| `Qwen/Qwen2.5-Coder-7B-Instruct` | ~4 GB | Yes | No — 8 GB card only |

**Current recommendation:** `Qwen/Qwen2.5-Coder-32B-Instruct` at 4-bit — highest
HumanEval score (92.7%) among open-source models that fit your hardware.
Check the leaderboards above for anything newer before committing.

---

## 6. Configure .env

```bash
BACKEND=huggingface
HF_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
HF_DEVICE_MAP=auto          # distributes across GPUs automatically
HF_TORCH_DTYPE=bfloat16     # standard for modern GPUs
HF_LOAD_IN_4BIT=true        # required to fit 32B in 24 GB
HF_LOAD_IN_8BIT=false
HF_MAX_NEW_TOKENS=4096
```

---

## 7. Verify the setup

```bash
python3 - <<'EOF'
import os
os.environ.setdefault("BACKEND", "huggingface")
from model_interact import ask
response = ask("Write a Rust function that returns the sum of a Vec<i32>.")
print(response)
EOF
```

On first run the model downloads (~18 GB) and loads into VRAM — this takes a few
minutes. Subsequent runs load from the local cache instantly.

When the model loads, the pipeline prints which devices are active:

```
[HuggingFaceBackend] Loading Qwen/Qwen2.5-Coder-32B-Instruct ...
[HuggingFaceBackend] Model loaded on: {'cuda:0'}
```

If you see `{'cuda:0', 'cuda:1'}` the model spans both GPUs.

---

## 8. Troubleshooting

**OOM on load:**
- Confirm `HF_LOAD_IN_4BIT=true` is set
- Reduce `HF_MAX_NEW_TOKENS` to `2048`
- Try a smaller model (e.g. Qwen2.5-Coder-14B)

**OOM during generation (long prompts):**
- The pipeline sends prompts up to ~20k chars — this is ~5k tokens, well within
  128K context, but KV cache can still OOM if VRAM is tight
- Reduce `CHUNK_CHAR_LIMIT` in `pipeline.py` as a last resort

**bitsandbytes CUDA error:**
```bash
pip install bitsandbytes --extra-index-url https://huggingface.github.io/bitsandbytes-wheels/
```

**Slow first call (expected):**
- Model loads on first `ask()` call (~1–3 min depending on model size)
- All subsequent calls are fast — model stays in VRAM for the whole pipeline run

**Model not found / 401 error:**
- Non-gated models (Qwen, DeepSeek) need no auth — double-check the repo ID spelling
- Gated models (Llama): run `huggingface-cli login`
