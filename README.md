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

# 2. Configure your model in .env (Ollama or Claude)
cp .env.example .env   # then edit MODEL= and optionally ANTHROPIC_API_KEY=

# 3. Strip a project down to tests only
python strip_to_tests.py <project_dir>

# 4. Run the pipeline
python pipeline.py --project <project_dir>
```

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
    model_interact.py              -- model wrapper (Anthropic or Ollama, auto-selected)
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

Copy the example env file:

    cp .env.example .env

Then edit .env to choose your model:

    # To use Ollama locally (free, no API key needed):
    MODEL=qwen2.5-coder:7b

    # To use AI (requires API key from console.anthropic.com):
    MODEL=claude-haiku-4-5-20251001
    ANTHROPIC_API_KEY=your_key_here

If using Ollama, install it from ollama.com and pull the model first:

    ollama pull qwen2.5-coder:7b

#### Listing available Ollama models

To see what models are already downloaded:

    ollama list

To search for models on the Ollama library (requires a browser or curl):

    # open https://ollama.com/library in a browser, filter by "code"
    # or search via CLI:
    ollama search deepseek-coder

#### Recommended models for code reconstruction

The pipeline works with any Ollama model. Larger models produce significantly better
results. Choose based on your hardware (RAM / VRAM):

| Model | Pull command | Size (Q4) | Min RAM | Rust quality | Notes |
|-------|-------------|-----------|---------|--------------|-------|
| `codestral:22b` | `ollama pull codestral` | ~13 GB | 15 GB | ★★★★★ | Best code model at this size; needs most of available RAM |
| `deepseek-coder-v2:16b` | `ollama pull deepseek-coder-v2:16b` | ~9.5 GB | 12 GB | ★★★★★ | Excellent Rust; strong instruction following |
| `qwen2.5-coder:14b` | `ollama pull qwen2.5-coder:14b` | ~8.5 GB | 10 GB | ★★★★☆ | Direct upgrade over 7b; same architecture |
| `starcoder2:15b` | `ollama pull starcoder2:15b` | ~9 GB | 10 GB | ★★★★☆ | Strong on code completion; large context |
| `deepseek-r1:14b` | `ollama pull deepseek-r1:14b` | ~8.5 GB | 10 GB | ★★★☆☆ | Adds reasoning chains; slower per token |
| `qwen2.5-coder:7b` | `ollama pull qwen2.5-coder:7b` | ~4.7 GB | 6 GB | ★★☆☆☆ | Default; fits on most GPUs; limited on large projects |

**codestral** is Mistral's dedicated code model and one of the best available on Ollama for
generating correct, structured code across many languages including Rust. At ~13 GB it is
tight on a 15 GB machine — if you see OOM errors, use `deepseek-coder-v2:16b` instead.

Update `MODEL=` in `.env` after pulling, then restart the pipeline.

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
