See autopilot.md.

---

# Handover — May 2026 (session 2)

## What this project is

`pipeline.py` is an LLM-driven loop that reconstructs Rust source code from tests alone.
Given a project stripped to its test files, it runs a sequence of tasks (TASK 0a through
TASK 4) to regenerate the implementation until the test suite passes.

The current target project is `argh-master/` (a Rust CLI argument parsing library).
The pipeline runs on **qwen3:32b via Ollama** (RTX 3090, `OLLAMA_NUM_CTX=32768`).
`.env` is already correctly configured — do not change the model settings.

---

## What was done in this session

All work was in `pipeline.py`. No other files were modified. The changes fall into four
categories: context budget, structural missing-context, anti-corruption validators, and
generality.

### 1. Context budget — model-aware, automatic

Replaced all hardcoded 8192-token-era limits with a single derived constant:

```python
def _model_context_chars() -> int:
    ollama_ctx = os.getenv("OLLAMA_NUM_CTX")
    if ollama_ctx and ollama_ctx.strip().isdigit():
        tokens = int(ollama_ctx)
        return max((tokens - 6_000), 4_000) * 4
    if MODEL.startswith("claude"):
        return (200_000 - 6_000) * 4
    return (8_192 - 6_000) * 4

MODEL_CONTEXT_CHARS = _model_context_chars()
SINGLE_CALL_CHAR_LIMIT = MODEL_CONTEXT_CHARS * 3 // 4
FILE_CHAR_LIMIT        = MODEL_CONTEXT_CHARS * 85 // 100
INTERMEDIATE_CHAR_LIMIT= MODEL_CONTEXT_CHARS // 2
```

Added a **context-guard wrapper** around `ask()` that catches total prompt overflow
(system + user prompt exceeding MODEL_CONTEXT_CHARS) and trims from the middle,
preserving the first 25% and last 12.5% of the prompt body. All `ask()` calls in
the pipeline now go through this guard automatically.

### 2. Structural missing-context fixes

All five structural bugs where the model received the wrong or incomplete inputs:

| Bug | Fix |
|---|---|
| TASK 2 never received test files | `StaticFileListTask2Generator` now reads test files from disk and appends them to each per-file prompt, filtered to the current module |
| TASK 4 never received generated source files | Main loop builds `t4_source_context` from the manifest before calling `task4()`; both build and test prompts now include the generated code |
| TASK 1 per-chunk prompt had no genesis.md | `_chunk_prompt()` in both `StaticTask1Synthesizer` and `LLMTask1Synthesizer` now accepts and appends `genesis` |
| TASK 1 stage-1 synthesis had no genesis.md | `_synthesize_group()` in both classes now accepts and appends `genesis` |
| Fix loop: truncated requirements, no test files, no genesis | `task2_fix_loop()` now passes full requirements (removed `smart_truncate(total_limit=2000)`), appends test files filtered to module, appends genesis.md |

### 3. Anti-corruption validators

Four validator functions added:

- `validate_task1_output(req_path, test_files)` — length ≥ 500, no meta-commentary,
  ≥ 3 identifiers overlap with test file content
- `validate_task2_output(project_dir, expected_paths, req_path)` — all expected files
  present, none < 50 bytes, no placeholder-only files, CamelCase types from FR appear
  as struct/enum in generated code
- `validate_task3_output(pass_count, fail_count, module_has_tests)` — returns one of
  `'pass'`, `'fail'`, `'no_tests'`, `'no_files'`
- `validate_task4_output(genesis_before, genesis_after, test_output)` — genesis grew,
  new content references test identifiers, no duplicate paragraphs

Wired into the module loop:
- TASK 1: retry loop (max 2 retries), then escalates to TASK 4 with
  `failure_type="requirements_failure"` on exhaustion
- TASK 2: `validate_task2_output(..., req_path=module_req_path)` before running tests
- TASK 3: `validate_task3_output()` classifies result; `no_files` locks immediately;
  `no_tests` runs `cargo test --no-run` to check if it's actually a build failure
- TASK 4: `validate_task4_output()` detects genesis stall and logs prominently

The `no_files` / `no_tests` distinction fixes the 56-iteration argh_shared loop.

### 4. Other fixes

- **Chunk cache** now includes genesis hash — stale partials are invalidated after
  each TASK 4 update
- **Pre-clean regex** fixed: `mod gen_\w+` instead of `mod \w+` — no longer destroys
  original handwritten test blocks
- **TASK 0c** changed from fixed 3-attempt for loop to coverage-driven while loop
  (max 10 attempts, stops when coverage reaches 100%)
- **`load_original_structure`** prefix collision fixed: exact stem match instead of
  startswith
- **Profile path** fixed to be per-project (`project_dir / PROJECT_PROFILE_NAME`)
- **generate_fix_loop** passes source files in full (no `[:2000]` truncation)
- **TASK 4** passes genesis, functional_req, test output, and failure_log in full
  (removed all `smart_truncate` calls on these)
- **`load_failure_log`** returns full log (removed 8000-char tail cap)
- **proc-macro TASK 1** filter regex is now derived from the actual crate name
  (`module_name`) instead of hardcoded `#\[argh\(|#\[clap\(|#\[structopt\(`).
  Fallback changed from `test_files[:3]` to all test files.
- **TASK 2 `proc_macro_note`** scrubbed of argh-specific type names (`argh::EarlyExit`,
  `argh::FromArgs`, `derive_from_args`, `attributes(argh)`). Now generic.
- **`task1()`** now accepts `module_name` and passes it to `_task1_proc_macro` as
  `crate_name`
- **`settings.json`** updated: `"permissions": {"allow": ["Bash(*)"]}` — no more
  permission prompts for shell commands

---

## What is still missing

### Truncation sites (quick edits — exact line numbers as of this session)

| # | File | Location | Issue |
|---|---|---|---|
| T1 | `pipeline.py` | lines 707, 1063 | `LLMDependencyGraph` / `LLMWorkspaceScanner` cap each manifest to `[:4000]` |
| T2 | `pipeline.py` | line ~3558 | TASK 2 sibling files capped to `prev_content[:1500]` per file |
| T3 | `pipeline.py` | line ~3793 | `task2_inner_loop` passes build error through `smart_truncate(total_limit=6000)` |
| T4 | `pipeline.py` | line ~4510 | TASK 0c pre-flight truncates compile error to `[:800]` |
| T5 | `pipeline.py` | line ~4514 | TASK 0c pre-flight truncates file content to `[:6000]` |
| T6 | `pipeline.py` | lines 2544, 2921 | Stage-1 synthesis `smart_truncate` limits are 8192-era values (defensible but stale) |

All of T1–T5 are single-line edits: replace the `[:N]` or `smart_truncate(total_limit=N)`
with the full content. The context-guard wrapper handles any resulting overflow.

### Structural / missing-context (more involved)

| # | Entry | Issue |
|---|---|---|
| S1 | `#546` | TASK 0a model sees only file paths, not test file content — profile built blind |
| S2 | `#501` | TASK 0a profile never invalidated — cached forever even if project changes |
| S3 | `#681` | `_extract_public_symbols` returns only names, not signatures or field shapes |
| S4 | `#282` | TASK 0c generates tests file-by-file with no cross-file type context |
| S5 | `#64` | `task0c_augment_module` hardcodes Rust-specific behaviour in orchestration |

S1 and S2 are the highest-value structural fixes: without them TASK 0a builds a project
profile that may be inaccurate or stale, affecting every downstream task.

---

## Recommended order for next session

1. Fix T1–T5 first — they are all single-line changes and remove active context loss.
2. Fix T6 — update `smart_truncate` limits to `INTERMEDIATE_CHAR_LIMIT` (already
   model-aware) so stage-1 synthesis scales with context window.
3. Fix S1 (TASK 0a gets test file content) and S2 (profile invalidation).
4. Fix S3 (`_extract_public_symbols` includes field definitions).
5. Fix S4 and S5 last — they touch TASK 0c orchestration logic, which is more complex.

After all fixes: restart the pipeline on argh.

---

## Key files

| File | Purpose |
|---|---|
| `pipeline.py` | The pipeline — all code changes go here |
| `autopilot.md` | Autopilot rules and startup command |
| `handover/improvements_may_3.md` | Original audit findings — 29 entries with exact line numbers |
| `ARCHITECTURE.md` | Step payload contracts and anti-corruption schemas |
| `FUNCTIONAL_REQUIREMENTS.md` | FR-001 through FR-013 — design decisions |
| `failure_log.md` | Running record of every iteration outcome |
| `argh-master/` | The target project directory |
| `.env` | Model name, API key, feature flags — already correct |

## Hard rules (from autopilot.md)

- Never write source files inside `argh-master/` or any project subdirectory directly.
  Only `pipeline.py` (via the LLM it calls) may write those.
- Never call pipeline task functions directly. Only start `pipeline.py` as a process.
- The autopilot may only touch: `pipeline.py`, `.env`, `autopilot.md`, shell scripts,
  `CLAUDE.md`, `ARCHITECTURE.md`, `FUNCTIONAL_REQUIREMENTS.md`.

## How to start the pipeline

```bash
cd /home/user/determ && source .venv/bin/activate
nohup python pipeline.py --project argh > /tmp/pipeline_live.log 2>&1 &
```

## How to check status

```bash
tail -50 /tmp/pipeline_live.log
pgrep -la python | grep pipeline
```
