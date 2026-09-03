# determ

**Reconstruct a software implementation from its tests alone — deterministically, with a
self-hosted LLM.** Strip a project down to its tests + frozen signatures, then drive a local
model through a fixed pipeline until the *original* test suite passes. The target is
test-passing behaviour, never source fidelity — the model never sees the original bodies.

The current reference target is Rust libraries; the methodology (captured in `genesis.md`) is
meant to generalise. The real output of the experiment is that methodology, not the rebuilt code
(see [Deterministic Process](#deterministic-process) below for the full framing).

> Research pipeline, mid-migration to a layered architecture. Design + live refactoring backlog:
> [`CODE_ARCHITECTURE.md`](CODE_ARCHITECTURE.md).

## Architecture

Two codebases with one sacred boundary between them.

| Part | What | Where |
|---|---|---|
| **Pipeline** (Python) | the LLM-driven reconstruction loop + layered core | `pipeline.py` (entrypoint) → `pipeline_core/` |
| **Extractor** (Rust) | `syn`-based AST extraction: skeletonise, enumerate fns/types/mods, cross-cluster merge | `tools/skeleton_extractor/` |
| **The seam** | a JSON CLI contract between them | [`docs/SEAM.md`](docs/SEAM.md) |

`pipeline_core/` follows clean-architecture layers, **enforced by import-linter** (a higher
layer may import a lower one, never the reverse):

```
domain      pure record types + rules (the wire contract, cfg eval). stdlib only.
ports       typing.Protocol interfaces the application depends on.
application use cases: the extracted task* functions.
adapters    the only layer that does I/O (LLM client, extractor binary, cargo).
__main__    pipeline.py — composition root: build Settings + adapters, inject, run.
```

**The strip boundary** is the defining constraint: after `strip`, neither the pipeline nor the
model may ever read the original source again. Everything source-derived (the `unimplemented!()`
skeleton, signatures, the cluster manifest) is extracted *once* before strip and saved to disk;
from `task1` on, the only inputs are the tests, those artefacts, and the model's own compile
errors. A **cluster** is one compiler configuration (stable / nightly / a cargo feature); the
pipeline is cluster-aware end-to-end. Self-hosted only — it never calls a cloud LLM.

## Development

Every change must keep `bash check.sh` green: mypy + ruff + the import-linter layer contract +
the extraction-invariant checker + pytest (with a domain-coverage floor). The Rust crate is gated
by `cargo fmt --check`, `clippy -D warnings`, `cargo test`, `cargo doc`. CI runs both — see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Quick start

```bash
# 1. Activate the virtual environment
source .venv/bin/activate

# 2. Configure the backend + model in .env (see "Serving the model" below)
#    BACKEND=vllm
#    MODEL=RedHatAI/Muse-Glimmer-30B-FP8-block
#    PIPELINE_PARALLELISM=4

# 3. Start the local server (vLLM in Docker, across both GPUs)
bash docker_vllm.sh

# 4. Strip a project down to tests only
python strip_to_tests.py <project_dir>

# 5. Run the pipeline
python pipeline.py --project <project_dir>
```

---

## Serving the model

Self-hosted only — the pipeline never calls a cloud LLM. Three transports are
implemented (`pipeline_core/adapters/llm_transports.py`): **vLLM** (the active one),
**Ollama**, and **SGLang**. `BACKEND=` in `.env` picks one.

### The one switch

`MODEL=` is the only line you change to move the whole stack between models. Everything
else derives from it: `TASK*_MODEL` each fall back to `MODEL`, `VLLM_MODEL_NAME` defaults
to it on both the Python and the launcher side, and `docker_vllm.sh` picks the server
image, the reasoning parser and the tool-call parser from the model string. The same
string is what `MODEL_CAPS` keys on, so the request dialect follows too.

| Model | Reasoning dialect | Server image | Parsers |
|---|---|---|---|
| `RedHatAI/Muse-Glimmer-30B-FP8-block` *(default)* | `reasoning_strength`: `low` / `medium` / `high` / `xhigh` — no on/off boolean, so "not thinking" is the `low` rung (`VLLM_REASONING_STRENGTH`, `VLLM_REASONING_STRENGTH_OFF`) | `vllm/vllm-openai:muse-glimmer` | `muse_glimmer` reasoning + tool-call |
| `Qwen/Qwen3.6-27B-FP8` | `enable_thinking`: a boolean its chat template branches on, so `false` actually suppresses the think block | `vllm/vllm-openai:latest` | `qwen3` reasoning, no tool-call parser |

The default is Muse Glimmer on one measurement: the 2026-08-12 A/B matrix over identical
crates and seeds put **output-contract compliance at 96–100% for Muse against 41–58% for
Qwen** — Qwen ignored the FN/FILE output contract about half the time — and Muse completed
the microcrate and hardcrate smokes at equal or better scores. Both models sit at the same
83-of-106 plateau on a cold `anyhow` tree, so that plateau is a pipeline property, not a
model ceiling. See `QwenVSMuse.txt`.

### Why vLLM, on two GPUs

The box is **2 × RTX 3090, 24 GB each**. A 27–30B checkpoint at FP8 does not fit one card
with a 131k-token window, so the server shards it: `--tensor-parallel-size 2`
(`VLLM_TENSOR_PARALLEL=2` in `.env`), `NCCL_P2P_DISABLE=1`, `--gpu-memory-utilization 0.90`.
Measured from the engine's own startup line at TP=2 on this hardware (2026-08-11):
**189,405 tokens** of GPU KV cache for Muse Glimmer, **136,533** for Qwen3.6-27B-FP8 —
which is why `VLLM_MAX_MODEL_LEN=131072` is honest for both rather than a guess.
`docker_vllm.sh` refuses to launch a longer window than the box has measured room for,
so a forgotten edit is an error at launch instead of a KV-allocation OOM later.

**That sharding is what buys the parallelism.** `PIPELINE_PARALLELISM=4` is the single
global concurrency knob — it governs the task0c split-and-merge degree, the phase-C fn-fill
workers, and concurrent task2a clusters — and it is set to the number the server is started
with, `--max-num-seqs 4`. Four fills are in flight as one continuous batch on the two cards;
splice and `cargo` stay serialised per source tree, and parallel clusters own isolated
trees, so throughput goes up without two writers ever meeting in one file. `--enable-prefix-caching`
is what makes it pay: the fill prompts share a long common prefix, so the batch reuses it
instead of re-attending it four times. Drop both to `1` to compare quality against the
historical sequential runs.

Ollama has no equivalent here: no tensor-parallel shard of an FP8 checkpoint across the two
cards, and no `--max-num-seqs` batch for `PIPELINE_PARALLELISM` to be matched against — the
knob would just queue. It stays as the low-setup path for a small model on one card; keep
`PIPELINE_PARALLELISM=1` on it.

### Running the server

```bash
bash docker_vllm.sh                    # start it (reads .env; model load + CUDA graphs: 10-20 min)
bash docker_vllm.sh --y                # stop whatever is running first, then start
bash docker_vllm.sh --allow-download   # let vLLM fetch a model the HF cache does not hold
bash docker_vllm.sh --no-pull          # offline restart: keep the image copy on disk

curl -s localhost:8000/version         # which server is actually serving
watch -n1 nvidia-smi                   # headroom: ~+1-2 GB of KV per +8192 tokens, across both cards
```

It pulls the image tag on every launch by default, because `docker run` never re-fetches a
tag it already holds — this box served a three-month-old vLLM that way, and a model needing
a newer engine fails as "unknown architecture" rather than "your image is stale". The
corollary: **restarting the server also upgrades it**, so leave a healthy container alone in
the middle of a measurement you intend to compare against an earlier run.

---

# Deterministic Process

Our goal is to find a universal set of rules, a methodology for describing software to an AI so that it can rebuild it completely, correctly from scratch in a deterministic way.
This means, full product repositories will be composed of 4 artifacts:
1. README.md
2. business_requirements.md
3. functional_requirements.md
4. automated tests
No source code. the pipeline will build the whole product, tests are going to pass each time, the product could have some small differences, but it will be mathematically and logically equivalent.

The methodology is captured in genesis.md. Starting from an empty genesis.md, we're going to build the right methodology following an iterative process of continuously improving. We will train the system by cloning the most complex repositories that have good code coverage. In the iteration we'll answers one question: given only the tests of a
project, what reasoning process should an AI follow to produce functional requirements precise
enough to guarantee a passing implementation?

The training works by:

1. Taking a real, complex project and removing all source code, leaving only tests
2. Letting AI attempt to reconstruct the source code from the tests alone
3. Running the tests,  they fail, because the reconstruction is imperfect
4. AI studies the failures and updates its methodology in genesis.md
5. Repeat until all tests pass

Each failure teaches AI a general lesson: not "I missed function X" but "I was not reading this class of signal from tests." Over many iterations, genesis.md becomes a mature,
project-independent methodology.

The hypothesis is that once mature, this methodology works on any project in any language on the
first or second attempt, without needing to iterate. The experiment ends when all tests pass,
but the real output is genesis.md, not the reconstructed code.

## Setup

Remove all source code from a project (any language), keeping only the tests and
README.md. Then run the pipeline script. It handles everything from there.
The language, framework, and project structure are derived by AI from those
two inputs alone -- no additional hints are needed or provided.

## Files

    deterministic_process.md       -- full description of the experiment and loop
    model_interact.py              -- model wrapper (Ollama / vLLM / SGLang / HF)
    pipeline.py                 -- orchestrates the full loop
    strip_to_tests.py              -- strips a project down to test code only
    requirements.txt               -- Python dependencies
    genesis.md                     -- created and refined by AI across iterations (persists)
    functional_requirements.md     -- rebuilt by AI each iteration (deleted on failure)
    tests_output_{N}.txt           -- genesis snapshot + test output from iteration N
    run.log                        -- full audit trail of all iterations across restarts
    <project>/.generated_files.txt -- manifest of source files written by AI

## Step-by-step setup

### Step 1 -- Create a virtual environment

Python's built-in venv is used. No extra tools required.

    python3 -m venv .venv
    source .venv/bin/activate        # bash/zsh
    source .venv/bin/activate.fish   # fish
    # Windows: .venv\Scripts\activate
    pip install -r requirements.txt

Activate the environment in every new terminal session before running any script:

    source .venv/bin/activate

### Step 2 -- Configure your model

There is no `.env.example` to copy — `.env` is untracked and you write it. The keys that
decide the run:

    BACKEND=vllm                                   # vllm | ollama | sglang
    MODEL=RedHatAI/Muse-Glimmer-30B-FP8-block      # the one switch (see "Serving the model")
    VLLM_BASE_URL=http://localhost:8000/v1
    VLLM_TENSOR_PARALLEL=2                         # shard across both cards
    VLLM_MAX_MODEL_LEN=131072                      # MUST match what the server was started with
    PIPELINE_PARALLELISM=4                         # matched to the server's --max-num-seqs

`VLLM_MAX_MODEL_LEN` is the one key the model switch does not carry: the Python budget math
reads it directly and its own fallback is 65536, so deleting the line would quietly halve the
client's context while the server kept serving the full window. `.env` also holds `HF_TOKEN`
if your model repo is gated — it is untracked for that reason; do not commit it.

Then start the server and check it answers:

    bash docker_vllm.sh
    curl -s localhost:8000/v1/models

#### The Ollama path

Lower setup, one card, no batching — use it to try the loop on a small model, not to run a
real reconstruction:

    BACKEND=ollama
    MODEL=qwen2.5-coder:7b
    OLLAMA_NUM_CTX=32768          # the transport's own default is only 8192
    PIPELINE_PARALLELISM=1

Install Ollama from ollama.com, then `ollama pull qwen2.5-coder:7b`; `ollama list` shows what
is already downloaded. Models that need Ollama's native `/api/chat` endpoint rather than the
OpenAI-compatible one are marked `native_ollama` in `MODEL_CAPS`
(`pipeline_core/adapters/llm_transports.py`) — `qwen3.6:27b` is one of them, and routing it
through the compat layer returns an empty string whenever thinking is on.

#### Models this pipeline has actually been run on

| Model | Backend | Size | Reasoning dialect | Notes |
|---|---|---|---|---|
| `RedHatAI/Muse-Glimmer-30B-FP8-block` | vLLM, TP=2 | 30B FP8 | `reasoning_strength` (low/medium/high/xhigh) | **Current default.** 96-100% output-contract compliance in the 2026-08-12 A/B; 189,405-token KV cache on 2x3090 |
| `Qwen/Qwen3.6-27B-FP8` | vLLM, TP=2 | 27B FP8 | `enable_thinking` boolean | 41-58% contract compliance on the same matrix; 136,533-token KV cache |
| `mattbucci/Qwen3.6-27B-AWQ` | vLLM | 27B AWQ | `enable_thinking` | Smaller-footprint quantisation of the same model |
| `qwen3.6:27b` | Ollama (native endpoint) | 27B Q4 | `enable_thinking` | Needs `/api/chat`; the compat path returns empty with thinking on |
| `qwen3.6-27b-q8` | SGLang | 27B Q8 | `enable_thinking`, budgeted via `thinking_budget` | Same model as the row above; the SGLang served name uses a dash, not a colon |
| `qwen2.5-coder:7b` | Ollama | 7B Q4 | unlisted in `MODEL_CAPS`, so it takes the `enable_thinking` default | The code's fallback `MODEL`; a smoke/first-run model, too small for a real reconstruction |

Take the cheap validation first: `bash run_microcrate.sh` on a new model before spending hours
on a real crate — a model that ignores the FN/FILE output contract fails in minutes there
instead of looking like a slow model six hours in.

### Step 3 -- Prepare the project

Take any project in any language and place it as a subfolder next to pipeline.py.
The subfolder must contain a README.md.

    determ/
      pipeline.py
      strip_to_tests.py
      agave/          <-- your project here

### Step 4 -- Strip to tests only

Remove all implementation code, keeping only test files, build files, and README.md.
Use the provided strip_to_tests.py script:

    python strip_to_tests.py <project_dir>

Example:

    python strip_to_tests.py agave

This will:
- Delete all source files with no test code
- Strip source files that contain test blocks down to the test blocks only
- Leave pure test files (tests/ directories), build files (Cargo.toml etc.), and README.md untouched

After this step, the project will compile but all tests will fail -- there is no
implementation. This is the correct starting state for the pipeline.

### Step 5 -- Run the pipeline

    python pipeline.py --project <subfolder>

Example:

    python pipeline.py --project agave

The pipeline loops until all tests pass. On each iteration it reads the test
files, generates source code, runs the tests, and refines its methodology (genesis.md).
It resumes automatically from where it left off if interrupted.

## Usage (short form)

    python pipeline.py --project <subfolder>

The subfolder must exist inside the same directory as pipeline.py and contain a README.md.

## How it works

On startup the pipeline:
- Reads README.md, asks AI for the shell command to run the tests, stores it.
- Scans the project for all test files and keeps them in memory. The set of their
  paths is also stored to prevent AI from overwriting them in TASK 2.
- Checks for a leftover manifest from an interrupted previous run and cleans up
  any partial source files before starting.
- Checks for existing tests_output_N.txt files and resumes from the next iteration
  if found.
- Opens run.log for append. All output goes to both stdout and this file.

Then it loops until all tests pass:

TASK 1 -- AI receives all test files + README + current genesis.md.
          If the test files are too large for one API call, they are split into
          chunks. Each chunk is analyzed separately (with its own retry), then a
          synthesis call combines the results into the final documents.
          AI produces genesis.md and functional_requirements.md.
          Retried up to 3 times on failure.

TASK 2 -- AI receives functional_requirements.md and genesis.md. It derives the
          language, framework, and file structure from those documents with no
          additional hints. The pipeline writes the produced files to disk and
          records them in a manifest. Any path matching an existing test file is
          skipped -- test files are never overwritten.
          Retried up to 3 times on failure.

TASK 3 -- The pipeline runs the test command and saves output to tests_output_{N}.txt.
          Each file begins with the genesis.md that produced it, making it self-contained.
          AI is not involved in this step.

TASK 4 -- AI receives the current test output and, when N > 1, the previous one.
          It compares the number of passing tests to detect improvement or regression.
          If the last change had no effect or made things worse, AI must take a
          substantively different approach. It produces an updated genesis.md.
          Test output is truncated intelligently: both the beginning (build errors,
          first failures) and the end (summary) are preserved.

TASK 5 -- The pipeline deletes functional_requirements.md.
          genesis.md is the only generated file that survives across iterations.

TASK 6 -- The pipeline deletes only the files it generated (tracked in the manifest).
          Test files are never touched. Next iteration begins.

If all tests pass after TASK 3, the process is complete.

## Monitoring with analysis.ipynb

`analysis.ipynb` is a Jupyter notebook for inspecting a live or completed pipeline run.
It is not a live dashboard — you run cells on demand to get a snapshot.

**To open it:**

Option 1 — browser (JupyterLab):

    source .venv/bin/activate        # bash/zsh
    source .venv/bin/activate.fish   # fish
    jupyter lab

Then open `http://localhost:8888` in your browser and click `analysis.ipynb`.

Option 2 — VS Code: open `analysis.ipynb` directly. Select the `.venv` kernel when prompted.

**Sections:**

| Section | What it shows |
|---------|---------------|
| 1. Setup | Configure paths (`ROOT`, `PROJECT`, `LOG_FILE`) before running anything else |
| 2. Pipeline status | Whether the pipeline process is running; last 50 lines of the log |
| 3. TASK 0c analysis | Per-file attempt counts, top compile error patterns, outcome timeline chart |
| 4. SQLite store | Type difficulty signals, compile patterns by module, genesis rules, module pass/fail trend |
| 5. run.log analysis | Coverage % over time, context-guard trim events |
| 6. Prompt prototyping | Send ad-hoc prompts to Ollama to test prompt changes before wiring them in |
| 7. Quick helpers | Per-file attempt history, `coverage_strategy.md` viewer, genesis diff between iterations |

The same information is available in the terminal without the notebook:

    tail -f /tmp/pipeline_anyhow.log          # live log stream
    cat anyhow-master/.task0c_attempts.jsonl  # attempt history (one JSON line per attempt)

The notebook is mainly useful for the charts and the SQLite queries.

---

## model_interact.py

Can also be used standalone to send a prompt to the configured model:

    python model_interact.py "your prompt here"
    python model_interact.py --file prompt.txt
    echo "prompt" | python model_interact.py
    python model_interact.py "prompt" --output response.txt
    python model_interact.py "prompt" --system "system prompt"

---

## License

Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 Virgilio Lino (Humanetes).
