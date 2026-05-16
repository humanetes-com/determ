# Pipeline Investigation Checklist

Use this when the pipeline seems slower, produces worse code, or has a high failure rate.
Work through these sections in order — the most impactful issues tend to surface first.

---

## 1. Check the Log

```bash
tail -200 run.log
# or for a background run:
tail -200 /tmp/pipeline_anyhow.log
```

### What to look for

| Pattern | Meaning | Action |
|---|---|---|
| `Project inputs changed — regenerating project profile` on every restart | Profile hash is being invalidated by task0c-generated files | Check `_profile_inputs_hash` filter; ensure only `tests/` files are hashed |
| `Empty generation response for <file> — retrying` on most files | Model exhausted `max_tokens` on thinking, nothing left for code | Check `MAX_TOKENS` derivation; verify it's ~`OLLAMA_NUM_CTX - 8192` |
| `No test block extracted for <file>` after retry | Both primary + follow-up failed to produce extractable code | Check prompt format, extraction regexes, and whether thinking is stripping correctly |
| Task 0a/0b re-running on every restart | Cache miss — hash mismatch or output file missing | Check hash files: `project_profile.inputs_hash`, `coverage_strategy.inputs_hash` |
| Fix attempt loop: `1 error(s) resolved … 1 remaining` then revert | Fix loop making partial progress but stuck | Check compile error log; may be a fundamental type/trait mismatch in coverage strategy |
| `LLM returned no fix — reverting` | Follow-up fix prompt also returned empty/unparseable | Same as empty generation; likely a max_tokens issue |
| Very long per-file durations (>3 min first attempt) | Model is spending all tokens thinking | max_tokens too low OR prompt too large (hits context guard) |
| Task 0c stalls at 0% improvement for 2+ rounds | Coverage not increasing despite test generation | Tests compile but don't exercise new lines; check which lines are uncovered |

### Timing sanity check
With Q8_0 on dual RTX 3090 at ~15-20 tok/s:
- 4,096 tokens → ~200-270 seconds (3-4 min) — if a file takes this long, max_tokens is being hit
- 32,768 tokens → ~27-36 min — unreasonably long per file; reduce if seen
- A healthy task0c file generation should be 30-90 seconds

---

## 2. Validate the Ollama Model

Query the running model to confirm sampling parameters are baked in correctly.

```bash
# Show model details — check parameters section
ollama show qwen3.6:27b-q8

# Confirm the model is loaded and responding
curl -s http://localhost:11434/api/tags | python3 -m json.tool | grep -A2 "qwen3.6"

# Quick sanity generation — should produce <think>...</think> then a short answer
curl -s http://localhost:11434/api/chat -d '{
  "model": "qwen3.6:27b-q8",
  "messages": [{"role":"user","content":"What is 2+2? Be brief."}],
  "stream": false
}' | python3 -m json.tool | grep -E "content|total_duration|eval_count"
```

### What `ollama show` should report
```
parameters:
  num_ctx       163840      ← matches OLLAMA_NUM_CTX in .env
  temperature   0.6         ← Qwen thinking mode recommendation
  top_p         0.95
  top_k         20          ← Qwen thinking mode recommendation
  min_p         0.05
  repeat_last_n 1024
  num_gpu       99
```

If any of these are missing or wrong: **rebuild the model** from `ModelfileHf`:
```bash
cd /home/user/determ && ollama create qwen3.6:27b-q8 -f ModelfileHf
```

### Validate thinking mode is working
The quick sanity curl above should return content that contains `<think>` in message.content
(compat layer) or a non-empty `message.thinking` (native ollama). For our compat setup,
look for `<think>` in the raw content before stripping.

```bash
# Raw response to check thinking tags are present
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b-q8",
    "messages": [{"role":"user","content":"Briefly explain what a Rust lifetime is."}],
    "max_tokens": 2048,
    "stream": false
  }' | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:500])"
```

Expected: output starts with `<think>` and ends with `</think>` followed by a short answer.
If output is empty or missing `<think>`: the model may have been built without the thinking template.

---

## 3. Check `model_interact.py` Configuration

```bash
cd /home/user/determ && source .venv/bin/activate
python3 -c "
import os
os.environ.update(open('.env').read().split('\n') and
    dict(l.split('=',1) for l in open('.env').read().splitlines() if '=' in l and not l.startswith('#')))
from model_interact import MAX_TOKENS, MODEL, MODEL_CAPS, _model_uses_think, _model_use_native_ollama
print(f'MODEL       = {MODEL}')
print(f'MAX_TOKENS  = {MAX_TOKENS}')
print(f'think       = {_model_uses_think(MODEL)}')
print(f'native_olm  = {_model_use_native_ollama(MODEL)}')
print(f'MODEL_CAPS  = {MODEL_CAPS.get(MODEL)}')
"
```

### What to verify
- `MAX_TOKENS` ≈ `OLLAMA_NUM_CTX - 8192` (currently ~155,648). If it shows 4096, the env var is not loaded.
- `think = True` for qwen3.6:27b-q8
- `native_olm = False` for qwen3.6:27b-q8 (compat path, thinking arrives as `<think>` tags in content)

---

## 4. Check `ModelfileHf` vs Running Model

Diff what's in `ModelfileHf` against what `ollama show` reports. They can diverge if the
Modelfile was updated but `ollama create` was not re-run.

```bash
ollama show qwen3.6:27b-q8 --modelfile 2>/dev/null | grep PARAMETER
grep PARAMETER /home/user/determ/ModelfileHf
```

Common divergence: `top_k` or `num_ctx` added to `ModelfileHf` but model not rebuilt.
Always run `ollama create qwen3.6:27b-q8 -f ModelfileHf` after any Modelfile change.

---

## 5. Check Cache Health

```bash
ls -la /home/user/determ/anyhow-master/project_profile.md \
         /home/user/determ/anyhow-master/project_profile.inputs_hash \
         /home/user/determ/anyhow-master/project_context.md \
         /home/user/determ/anyhow-master/coverage_strategy.md \
         /home/user/determ/anyhow-master/coverage_strategy.inputs_hash
```

All five files should exist. If `project_context.md` or `coverage_strategy.md` is missing,
the next run will re-generate it (one-time cost). If hash files are missing, both tasks
0a and 0b will re-run.

### Manually verify hash stability

Run this before and after a pipeline restart (without changing project files):
```bash
python3 -c "
import hashlib, os, sys
sys.path.insert(0, '/home/user/determ')
from pipeline import collect_test_files, _profile_inputs_hash
project = '/home/user/determ/anyhow-master'
readme = open(f'{project}/README.md').read()
tf = collect_test_files(__import__('pathlib').Path(project))
h = _profile_inputs_hash(readme, tf)
print(f'profile hash: {h}')
print(f'test files hashed: {len([r for r,_ in tf if \"tests/\" in r or r.startswith(\"tests\")])}')
"
```

Hash should be identical across restarts as long as `tests/*.rs` files haven't changed.

---

## 6. Check `.env` / Environment

```bash
cat /home/user/determ/.env | grep -v "^#" | grep -v "^$"
```

Key values and what they mean:

| Variable | Expected | Effect if wrong |
|---|---|---|
| `OLLAMA_NUM_CTX` | `163840` | Smaller → less context → task0b/0c prompts truncated |
| `MAX_TOKENS` | unset (auto-derived as ~155K) or explicit override | Too small (e.g. 4096) → thinking exhausts budget, empty code output |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Unset → f16 KV cache → higher VRAM usage, fewer tokens fit |
| `MODEL` / `TASK*_MODEL` | `qwen3.6:27b-q8` | Wrong model → different caps, thinking may not work |
| `BACKEND` | `ollama` | `sglang` uses different backend with `thinking_budget` parameter |

---

## 7. Review the Model Guidelines

```bash
cat /home/user/determ/knowledge/qwen3.6-27b-q8/guidelines.md
```

Cross-check the running configuration against the guidelines table (temperature, top_k, top_p,
max_tokens, min_p, context window). Any deviation is a candidate for investigation.

---

## Known Past Issues (Lessons Learned)

| Date | Issue | Root Cause | Fix |
|---|---|---|---|
| 2026-05-11 | `Empty generation response` on nearly every file | `MAX_TOKENS=4096` — thinking exhausted entire output budget | Derived `MAX_TOKENS` from `OLLAMA_NUM_CTX - 8192` (~155K) |
| 2026-05-11 | Tasks 0a/0b re-ran on every restart | `_profile_inputs_hash` included src files with inline `#[cfg(test)]` added by task0c | Hash now only covers `tests/` directory files |
| 2026-05-11 | Tasks 0a/0b re-ran on restart (task0b specifically) | task0b hash included generated `.rs` source files from previous run | Excluded manifest-tracked files from task0b hash |
| 2026-05-11 | Compat path follow-up slow and also failing | Follow-up included full thinking text as context, model re-reasoned with no thinking disabled | Follow-up now uses stripped thinking; native path uses `think=False` |
| 2026-05-11 | `native_ollama.chat(think=True)` returned 400 | `qwen3.6:27b-q8` built via `ModelfileHf` lacks native thinking protocol in template | Stays on compat path; thinking arrives as `<think>` tags in content |
| 2026-05-11 | High `Empty generation response` rate with qwen3.6:27b-q8 (133 in one session vs 36 in prior) | Three issues: (1) unclosed `<think>` when budget exhausted mid-reasoning — regex misses it, follow-up never triggered; (2) compat follow-up re-triggered reasoning because no `/no_think` directive; (3) minimal scaffold retry (attempt 1) didn't disable thinking | Fixed in `model_interact.py`: detect unclosed `<think>`, prepend `/no_think` on compat follow-up. Fixed in `pipeline.py`: pass `think=False` for attempt 1 retry |
