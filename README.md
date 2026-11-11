# determ

**Reconstruct a software implementation from its tests alone, with a self-hosted LLM.**
Take a real project, keep its tests, freeze its public signatures, delete every function
body, then drive a local model through a fixed pipeline until the project's own test
suite passes again. The target is test-passing behaviour, never source fidelity: after the
strip step the model, and the pipeline itself, never see an original body again.

The current reference target is Rust libraries, and the crate the pipeline is developed
against is [`anyhow`](https://github.com/dtolnay/anyhow). The methodology the run distils
(`genesis.md`, written by the pipeline into each workspace) is meant to generalise; see
[Deterministic Process](#deterministic-process) for the framing of the experiment.

> This is a research pipeline, not a product. It runs entirely on local hardware
> (developed on 2 × RTX 3090), it calls no cloud model, and a full run from scratch takes
> many GPU-hours. The design and the phase order are in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the conventions every change to this
> repository follows are in [`AGENTS.md`](AGENTS.md).

## Contents

- [Status](#status)
- [Architecture](#architecture)
- [Development](#development)
- [Requirements and setup](#requirements-and-setup)
- [Configuration (`.env`)](#configuration-env)
- [Quick start](#quick-start)
- [The phases](#the-phases)
- [The reconstruction loop (task 2a)](#the-reconstruction-loop-task-2a)
- [The end gate](#the-end-gate)
- [Self-healing: the loop as a supervision tree](#self-healing-the-loop-as-a-supervision-tree)
- [Stops, budgets and bounds](#stops-budgets-and-bounds)
- [Launch, resume, restart (anyhow)](#launch-resume-restart-anyhow)
- [Smoke crates and model comparisons](#smoke-crates-and-model-comparisons)
- [Serving the model](#serving-the-model)
- [Monitoring a run](#monitoring-a-run)
- [model_interact.py](#model_interactpy)
- [Deterministic Process](#deterministic-process)
- [License](#license)

---

## Status

The last run that took `anyhow` from scratch to every oracle test passing was launched on
commit `23b6967d489bc289cb9c22c8185e98bd5c750b2d` ("Tell a refuted body as history,
whatever body is on the tree"). Every leg of it records that commit as its
`pipeline_sha`:

| Leg | Run id | Started (UTC) | What happened |
|---|---|---|---|
| 1 | `336559-eb0222a5` | 2026-11-06 21:10 | From scratch, every model call at the maximum reasoning level. The behaviour harvest (task0d) took 16.7 h; task 2a went 60 → 68 → 76 passing in three fills (about 8.5 h). Stopped by hand 2026-11-08 06:10. |
| 2 | `3323407-984b07f2` | 2026-11-08 06:11 | Resumed on the same tree at `effort=medium, escalate-after=12`. Task 2a went 76 → 241 → 420 → green in 274 minutes. The process exited 1 at the end gate on one unreached stub (`write_str` of a trait impl nothing builds). |
| 3 | `479767-af5fb863` | 2026-11-08 15:43 | `SUCCESS: all tests passed on iteration 1`. |

That run is **not** a clean end-to-end result by this project's own bar: it was stopped
and resumed, and the resume ran working-tree changes (the reasoning level among them)
that were committed afterwards in `a6648be` and `5e88c2e`. The goal the pipeline is
judged against is a single from-scratch process, with no operator, that ends with every
oracle test green, then publishes, runs task 3 and exits 0. The end-gate defect that
stopped leg 2 is the subject of `a6648be` (see [The end gate](#the-end-gate)).

---

## Architecture

Two codebases with one sacred boundary between them.

| Part | What | Where |
|---|---|---|
| **Pipeline** (Python ≥ 3.12) | the LLM-driven reconstruction loop and its layered core | `pipeline.py` (entry point and composition root) → `pipeline_core/` |
| **Extractor** (Rust) | `syn`-based AST extraction: skeletonise, enumerate fns / types / mods, cfg evaluation | `tools/skeleton_extractor/` |
| **The seam** | a JSON CLI contract between them | [`docs/SEAM.md`](docs/SEAM.md) |

`pipeline_core/` follows clean-architecture layers, **enforced by import-linter**
(`[tool.importlinter]` in `pyproject.toml`; a higher layer may import a lower one, never
the reverse — adapters > application > ports > domain):

```
domain      pure record types and rules (the wire contract, cfg evaluation, every decision rule). stdlib only.
ports       typing.Protocol interfaces the application depends on.
application use cases: the task phases, the reconstruction loop, the end gate.
adapters    the only layer that does I/O (LLM transports, the extractor binary, cargo, git, files).
monitor     the terminal monitor's data and rendering (pipeline_core/monitor/).
__main__    pipeline.py: build settings and adapters, inject, run.
```

**The strip boundary** is the defining constraint. Before `strip`, the original crate is
read to harvest behaviour, grow the test suite and bake artefacts. After `strip`, nothing
in the pipeline reads original source again: no prompt, note, plan or log the model sees
carries an original body. Everything source-derived (the `unimplemented!()` skeleton, the
signatures, the API surface, the cluster manifest, the behaviour book) is extracted *once*,
before strip, and saved to disk. From `task1` on, the only inputs are the tests, those
artefacts, and what the compiler and the test binaries say about the tree the model wrote.

A **cluster** (also called a *world*) is one compiler configuration: `stable`, `nightly`,
a cargo feature set. For `anyhow` there are two, `stable` and `nightly`. Augmentation and
the behaviour harvest are per cluster; reconstruction is **one tree judged in every
world** (see [task 2a](#the-reconstruction-loop-task-2a)).

---

## Development

Every change must keep `bash check.sh` green. It runs, in parallel:

- mypy and ruff;
- the import-linter layer contract;
- the extraction invariants, fixture ambiguity, the identity contract;
- *tree writes* (every write into a fill tree goes through `tree_txn`);
- *crate symbols* (no executed string names a symbol of a reconstructed crate);
- the LOC ratchet (`.loc_ratchet.json`: the god-files only shrink);
- no `/tmp` literal outside `runtime_paths` (tests share `/tmp` with live runs);
- untyped-port keywords (no new keyword through a `Callable[..., X]` port);
- for the extractor: `cargo fmt --check`, `cargo clippy` + `cargo test` + `cargo doc`
  under the pinned gate toolchain;

then the pytest suite (xdist, 60 s per-test bound) and the coverage ratchet
(`.coverage_ratchet.json`: per-target floors that only rise, with `domain/` held at an
absolute 95 %). CI runs the same gates — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

`check.sh` is stage 2 of the launch gate; the whole gate is described under
[Gate](#gate). Tests that need real cargo are integration tests
(`tests/integration/`, `pytest.mark.integration`, skipped unless `INTEGRATION=1`); they
are run by `bash scripts/run_integration.sh` and by `bash full_pre_run.sh`.

Durable analysis scripts live in `scripts/` (about a hundred of them). Each opens with a
header that says why it exists, how to run it, and how to read its output.

---

## Requirements and setup

- Linux, Python 3.12+, Rust via `rustup` (stable and nightly toolchains; the crate under
  reconstruction pins its own in `rust-toolchain.toml`), `cargo-llvm-cov` for coverage,
  `gdb` for crash localisation, git.
- For the default backend: Docker with the NVIDIA runtime and enough VRAM to serve a
  27–30B FP8 model with a 131,072-token window. This repository is developed on
  2 × RTX 3090 (24 GB each), sharded with tensor parallelism 2.

```bash
bash setup.sh                     # idempotent: Rust, cargo tools, the Python venv and deps,
                                  # and the backend named by BACKEND in .env
# or, by hand:
python3 -m venv .venv
source .venv/bin/activate         # bash/zsh   (fish: source .venv/bin/activate.fish)
pip install -r requirements.txt
```

The launchers call `.venv/bin/python` directly, so the venv must live at `.venv` in the
repository root.

---

## Configuration (`.env`)

`.env` is not tracked (it is in `.gitignore`); you create it in the repository root. There
is no `.env.example`. Every knob has a default in code, so a minimal file is short:

```bash
# Backend and model — MODEL is the one switch (see "Serving the model").
BACKEND=vllm                         # vllm | ollama | sglang
MODEL=Qwen/Qwen3.8-27B-FP8           # or RedHatAI/Muse-Glimmer-30B-FP8-block
PIPELINE_PARALLELISM=4               # match the server's --max-num-seqs (docker_vllm.sh starts it with 4)

# vLLM client and server
VLLM_BASE_URL=http://localhost:8000/v1
VLLM_TENSOR_PARALLEL=2               # shard across both cards; docker_vllm.sh defaults to 1

# Only if the model repo is gated on Hugging Face. Use your own token.
# HF_TOKEN=...
```

Keys you may want, all optional:

| Key | Default | Meaning |
|---|---|---|
| `TASK0_MODEL`, `TASK0C_MODEL`, `TASK1_MODEL`, `TASK2_MODEL`, `TASK4_MODEL` | `MODEL` | Run one task on a different model. |
| `VLLM_MODEL_NAME` | `MODEL` | The served id, when it differs from the repo id. |
| `MODEL_FAMILY` | from the id | `qwen3` or `muse-glimmer`, for a repo spelled some other way. An id no profile matches is refused at launch. |
| `VLLM_MAX_MODEL_LEN` | the profile's (131072 for both families) | Context window. The pipeline and `docker_vllm.sh` resolve it the same way (env → profile → default), so the server and the client cannot disagree. |
| `VLLM_IMAGE`, `VLLM_REASONING_PARSER`, `VLLM_TOOL_CALL_PARSER` | the profile's | Server-side overrides. |
| `VLLM_THINKING_BUDGET`, `VLLM_REASONING_BUDGET`, `VLLM_RUNAWAY_THINK_CHARS`, `VLLM_MAX_THINK_CHARS`, `VLLM_CONTENT_BUDGET` | the profile's (`VLLM_CONTENT_BUDGET` 16384) | Reasoning and answer budgets. `VLLM_REASONING_BUDGET` is `fixed` or `per_call`; anything else raises. |
| `VLLM_REPETITION_PENALTY` | the profile's (Qwen 1.0, Muse 1.05) | Sampling repetition penalty. |
| `LLM_EFFORT`, `LLM_EFFORT_PHASES`, `TDD_ESCALATE_AFTER` | `medium`, none, 12 | Reasoning level per call; see [Reasoning levels](#reasoning-levels). |
| `OLLAMA_NUM_CTX` | 8192 | Ollama context; set 32768 for real use. |

The stop and budget knobs of the loop are listed in
[Stops, budgets and bounds](#stops-budgets-and-bounds). All of them are read once, at
cluster entry, and echoed on the cluster-entry log line, so a change takes effect at the
next launch and can be confirmed before any GPU time is spent. `.env` is outside the gate's
tree stamp: changing a knob needs no new gate run.

---

## Quick start

```bash
# 1. Configure .env (above), then start the local server (vLLM in Docker).
bash docker_vllm.sh                  # model load + CUDA graphs: 10–20 min
curl -s localhost:8000/v1/models     # check it answers

# 2. Pass the gate (extractor build, check.sh, gate contracts, walks).
bash pre_run.sh

# 3a. A first run on a new crate: clone it into tmp/<name>/ and run every phase.
.venv/bin/python pipeline.py --repo https://github.com/dtolnay/anyhow --clean --audit

# 3b. Or try the loop in minutes on the bundled micro crate first.
bash run_microcrate.sh
```

`--repo` clones (or copies a local path) into `tmp/<basename>/`, a git-managed workspace;
`--clean` deletes the generated state first. Once a workspace exists, it is resumed by name
(`--project <name>`), and for `anyhow` always through the launchers in
[Launch, resume, restart (anyhow)](#launch-resume-restart-anyhow), which add the gate
check, the model conformance check, detachment and the death record.

Take the cheap validation first on a new model: `bash run_microcrate.sh` before spending
hours on a real crate. A model that ignores the FN/FILE output contract fails there in
minutes, instead of looking like a slow model six hours in.

`strip_to_tests.py` is the standalone form of the strip step
(`python strip_to_tests.py <dir>`: deletes source files with no test code, cuts mixed
files down to their test blocks, leaves `tests/`, build files and `README.md`). The
pipeline runs its own strip phase; the script keeps a byte-equal copy of the workspace
layout rules (`pipeline_core/domain/workspace_layout.py`) so it can run without the
package.

Other `pipeline.py` flags: `--progress` (one rewriting progress line), `--nolog` (stdout
only), `--augment-tests` (force task0c), `--from-spec` (forward mode: skip task0a–task1 and
build from a `functional_requirements.md` you supply), `--restart-from-augmented`,
`--restart-from-pre-augment`, `--reharvest-behaviours` (the levers the launchers use; see
below), `--audit` (the flow auditor), and the standalone `--phase-a-only` …
`--phase-d-only` / `--skeleton-backend` subcommands. `python pipeline.py --help` prints
them all.

---

## The phases

Each phase records itself in `tmp/<project>/pipeline_state.md` as it completes; a resume
starts at the first phase not marked done. The phase one-liners the monitor shows are
maintained in `pipeline_core/monitor/legend.py`. A finished `anyhow` workspace reads:

```
[cfg_scan]  done          2 clusters: stable, nightly
[test_cmd]  done          cargo test
[task0a-context]  done
[task0a]  done
[task0b]  done
[task0d/stable]  done     260 behaviours, runtime then 230, exercises 209, probes 29/12
[task0d/nightly]  done    266 behaviours, runtime then 236, exercises 210, probes 35/16
[task0d/anyhow/book]  done  2 world(s) booked (nightly+stable), rule probes on nightly+stable
[task0c/stable/anyhow]  done   97.4%
[task0c/nightly/anyhow]  done  98.0%
[strip/anyhow]  done
[task1/anyhow]  done
[task2a/anyhow]  done     one tree, worlds nightly+stable
[task2b/anyhow]  done     13 file(s) published
[task2-4/anyhow]  done    iteration 1
```

**Understand the crate (the original is still on disk)**

| Phase | What it does |
|---|---|
| `cfg-scan` | No LLM. Turns cfg / feature / toolchain directives into build clusters. |
| `test_cmd` | Decides the test command (`cargo test`). |
| `task0a`, `task0a-context` | Project profile (`project_profile.md`) and architecture notes injected into later prompts (`project_context.md`). |
| `task0b` | Discovery: `coverage_strategy.md`, how to build, test and measure this crate. |
| `task0d` | **Behaviour harvest**, per cluster: test assertions, doc tests and layout pins from the original; probes run *on the original* with their stdout recorded as the expected result; exercises chosen by coverage. The behaviour book is the spec task0c and the layout author read. |
| `task0c` | **Coverage augmentation**, per cluster: the model adds unit tests (`*.gen_tests_*.rs`) until coverage reaches `COVERAGE_AUGMENTATION_TARGET`. Every original and every admitted generated test becomes the **oracle**. A test the original crate fails under the same cluster cfg cannot be made green and is withdrawn and logged. |

**Capture, then destroy**

| Phase | What it does |
|---|---|
| `bake` | The only read of original bodies: the skeleton with `unimplemented!()` stubs, the API surface, type and pub-item manifests, the cluster manifest, leaf-first fn order. |
| layout authoring (`[t1.5]` in the log) | The model designs the crate's private layout (struct fields, helper signatures) it will later have to fill. Accepted only if the skeleton still compiles with every body stubbed; a component that will not compile keeps its baked layout. |
| `strip` | Deletes the original implementation. From here on: tests and baked artefacts only. The oracle is sealed at strip, and a launch on a drifted oracle is refused (`HEALTH ‼ ORACLE_DRIFT`). |

**Reconstruct against the oracle**

| Phase | What it does |
|---|---|
| `task1` | Functional requirements (`functional_requirements_<crate>.md`) from the tests and the skeleton, including the model's own "Confusions to Avoid" notes. |
| `task2a` | **The reconstruction loop**: one loop over one tree (`.skeleton-filled/<default world>/src`), every test run repeated in every world and judged as one reading. Nearly every decision the pipeline makes lives here. |
| `task2b` | **Publish**: a byte copy of the tree's `src/**/*.rs` into the workspace `src/` (`application/reconstruct/publish.py`). No body is chosen, gated or written here, so `src/` is exactly the tree the loop judged. |
| `task2-4` (task3 / task4) | The strict oracle test run on the published crate (task3), the test-fix loop, and the [end gate](#the-end-gate). On failure, task4 analyses the failures into `genesis.md` rules and the best-tree ratchet keeps the highest-passing tree. |

Until 2026-10-19 task 2a ran one loop per cluster over one tree each, task2b merged the
trees under cfg gates and task2c filled the merged tree. On run `1680589-7a6824f8` the two
filled trees came out byte-identical, so the second tree bought nothing while keeping two
trees and a merge in step cost several defects. `cluster_plan.one_tree_cluster` now gives
the loop one identity: the default world's tree and stores, the merged view's cfg (an
item compiled in *any* world is in scope), and every test run repeated under each world.
`task2c` survives only as a restart alias of `task2b`.

Where things live in a workspace (`tmp/<project>/`):

| Path | What |
|---|---|
| `.skeleton/<cluster>/src` | the authored layout the bodies answer to |
| `.skeleton-filled/<cluster>/src` | **the crate the loop builds, tests and scores** |
| `.best_tree/<cluster>/src` | heal's durable restore tier |
| `src/` | after strip a shell (`*.gen_tests_*.rs` and a one-line `lib.rs`) until task2b publishes |
| `.phase_c_logs/` | convictions (`retrospectives.json`), ask ledgers, red ledgers |
| `genesis.md`, `functional_requirements_<crate>.md`, `project_*.md`, `coverage_strategy.md` | the spec documents |
| `pipeline_state.md` | the phase checkpoints |

All three tree directories are tracked in the workspace's own git repository. A layout
and its fills are a pair: bodies from one run do not compile against another run's
skeleton, so restore `.skeleton/<cluster>` and `.skeleton-filled/<cluster>` from the same
commit or not at all.

---

## The reconstruction loop (task 2a)

The driver is `pipeline_core/application/reconstruct/phase_c_tdd.py`
(`run_phase_c_tdd`). It is an outer loop of **sweeps** (a full test run of the tree in
every world) and **fills** (model calls that write bodies), and its whole state is printed
as one line per iteration. A real pair of iterations from a recent run:

```
[stable] [phase-c/tdd] iter 5: cargo test rc=101 (0.5s); pass=390 fail=11 oracle=401 (improved=True, stall=0/0); frontier=32 fn(s) (behavior), 1 skipped as exhausted; unowned=0; unseen=0; dispatching 12 of 32 (12/32 this pass, mid-pass). classes: cashed=3 wrong=4 mis=0 unreachable=3 dropped=0 compile=1 declined=0
[stable] [phase-c/tdd] iter 6: cargo test rc=101 (0.4s); pass=396 fail=5 oracle=401 (improved=True, stall=0/0); frontier=19 fn(s) (behavior), 1 skipped as exhausted; unowned=0; unseen=0; dispatching 12 of 19 (12/19 this pass, mid-pass). classes: cashed=2 wrong=3 mis=1 unreachable=0 dropped=0 compile=0 declined=0
```

That line is an API: the monitor reads it, walk fixtures are distilled from it, and a
stalled run is diagnosed by reading `frontier` and `exhausted` as a pair across
iterations, never from the pass count alone. The `classes:` field is the previous fill's
outcome per unit: **cashed** (verdicts moved), **wrong** (the right unit, a body that
compiles and is wrong), **mis** (mis-targeted: the unit's tests are blocked upstream),
**unreachable** (the right body, but the test cannot run yet), **dropped** (the model
answered and a route lost the answer — always a pipeline defect), **compile**, and
**declined**.

The cluster-entry line confirms every knob before any GPU time is spent:

```
[stable] [phase-c/tdd] driver=tdd, max_stalls=0 (plateau exit DISABLED), timeout=120s, stems=52, gpu-guard=off, no-frontier-stop after 2 undispatchable iteration(s), unseen-stop=1, fill-phase-stop=360 min, sessions-per-fill=4, session-min-turns=3, unit-bound=4x share, language-driver=generic, followup-think=off, replan-on-premise=on, asked-unchanged=on, cut-stop=4%/100 (4 cut call(s)), effort=medium, escalate-after=12 owed ask(s)
```

### What one iteration does

1. **Sweep.** `cargo test` over the tree in every world. With `PHASE_C_TEST_ISOLATION=1`
   (the default) an aborted test binary is re-run one test per process, so one abort
   cannot erase another test's verdict; every test gets one typed outcome (passed /
   failed with its own panic / aborted with a named cause).
2. **Attribution and the frontier.** Each red test is attributed to the units it fails
   through: measured coverage of the units that test executed, crash frames resolved by
   the debugger to the item that holds them (a closure frame is named by its creator),
   compile errors, and the stubs a test dies in. A test with no measured owner, no
   crash frame in the crate and no failure inside a crate body is *unseen*
   (`domain/test_sight.py`); the loop does not dispatch guesses at unseen tests.
3. **What is owed.** For every red test, `candidate_tiers.due_reading` walks its owner
   tiers, narrowest first, and owes the next unit not yet asked under the test's
   current evidence. `ask_fingerprint` is what the test says about itself (the failing
   assertion, the observed value, the failure site); `AskLedger.spent` stops offering a
   unit for a test once a body has landed under that fingerprint, or once enough offers
   landed nothing. While a native crash has a named culprit, `scope_to_crash_culprits`
   drops every other owner. `dispatch_rank` puts held stubs and fault sites first.
4. **The fill schedule** (`domain/fill_schedule.py`). The owed units are planned into
   routes — slice sessions (one conversation over a red test's executed slice), pairs,
   file-first rewrites, per-impl and per-fn fills — each with a share of the fill budget.
   Never-served routes rank before paying ones, and re-buys of routes that already
   bought nothing are deferred. The plan is printed on a `[phase-c/schedule] fill N:`
   line.
5. **Fill and judge.** Each reply is parsed into tagged FN/FILE blocks, spliced
   transactionally, compiled (an inner fix loop re-prompts with focused rustc errors),
   and judged against the reading before it. A keep that changed which tests fail, or
   where they first fail, ends the fill at the next claim
   (`PHASE_C_REPLAN_ON_PREMISE=1`), and the next sweep re-plans on the new facts.
6. **Iteration end.** The batch ratchet compares against the best tree, the stall
   counter and the flat-score guards update, and the stops in
   [Stops, budgets and bounds](#stops-budgets-and-bounds) are checked.

### What the model is told

Every call is self-contained. Prompts are built by one builder per route
(`build_file_fill_prompt`, `build_per_fn_prompt`, `build_per_impl_prompt`, the session
builder) from the spec documents, the skeleton and sibling context, the extractor's
structural facts (never regex), the failing test's own words (assertion, observed value,
first-failure site, crash excerpt) and the compiler's notes. Mid-task the model may pull
material with `<REQUEST kind="file" arg="src/error.rs"/>` (and `type_def` and friends);
the resolver answers within the prompt budget. Which documents reach which prompt is the
matrix in [`docs/PROMPT_CONTENT_AUDIT.md`](docs/PROMPT_CONTENT_AUDIT.md), pinned by
`tests/test_prompt_content_audit.py`.

A body that a test process crashed on is taken off the tree by the crash settlement and
recorded as **refuted**. Every later prompt for that function — per-fn, per-impl, pair,
file, session, and the task-3 test-fix loop — carries a note that states only history:
this body was taken off the tree because a test process crashed on it, and if the body
shown for this fn is the same text, it is the one that crashed. The note reads from the
ledger alone and makes no claim about the tree now, so it is true whatever body is on
the tree (commit `23b6967`). The previous form compared against the body on the tree
when the fill was planned, which hid the note exactly when a later settlement in the
same fill had put the crashing bytes back.

### When the loop closes

The loop closes **green** when every oracle test passes in every world. It closes
**red** through the `cluster_failed` exit: when every stub and every tier of every red
test has been asked under that test's current evidence (a body landed, or enough offers
landed nothing), nothing new can be asked, so the loop logs
`cluster_failed — … This cluster is NOT green:`, names each red test and its units, writes
the red ledger (`domain/red_ledger.py`) and returns exhausted. The phase after that is
publish and task 3 on the same tree, so every run ends with a per-test verdict, never a
silent stop.

---

## The end gate

A green suite is not yet success. The end gate (`orchestrator.remaining_stub_verdict`,
called by both completion paths) scans the published tree for bodies that are still
`unimplemented!()` and sorts each into a bucket:

- **unreachable** under every baked cfg-set — no world compiles it;
- **upstream hollow** — the original crate leaves it hollow too;
- **unreached by tests** — augmentation measured that no test executes it, or proved it
  unreachable by construction;
- **dead in tree** — nothing in the rebuilt crate or its oracle can call it, *proved by
  the compiler* (below);
- **orphaned** — reachable and implemented in the original. Any orphan fails the run:
  `WARNING: all tests passed, but N fn(s) are STILL unimplemented!()`.

The *dead in tree* proof (`domain/stub_removal.py`, `adapters/stub_removal.py`,
`application/unreached_stub_reading.py`) is a removal on a scratch copy. A free fn or
inherent method is its own unit; a trait-impl method belongs to its whole impl block.
rustc's `dead_code` lint seeds the dead set; a fixed point over MIR uses chooses the
remaining candidates and their bundles. Each stub's bundle is then deleted on a copy, and
the proof stands only when:

1. the crate and every test target still build (`cargo check --lib --tests` under the
   world's env and toolchain), **and**
2. every fn that survives the deletion has the **same MIR** as the baseline copy, in the
   library and the test profile (`-Zunpretty=mir`, `domain.stub_removal.mir_unchanged`).

The MIR condition exists because a build-only certificate accepted deletions that change
the program: deleting a held type's `Drop` impl builds, and removes the drop the compiler
inserted from its holder's MIR; deleting an impl a call reached through `Deref` builds,
and retargets the call to the inner method. Both are now kept, and no trait is named
anywhere in the rule. Deletion **blanks** instead of cutting — every character of a
deleted item becomes a space and its newlines stay — because the test harness stores a
test's line as a MIR constant, so a cut would move positions in items no deletion
touched (commit `a6648be`). Nothing in the end gate reads the original crate, and nothing
it returns reaches a prompt.

---

## Self-healing: the loop as a supervision tree

The reconstruct loop borrows its recovery model from Erlang/OTP supervisors. A supervisor
owns children at a scope. It **detects** that a child deviated, **restarts** it, and
**escalates** to its parent when restarts recur. One OTP assumption does not hold here,
and it shapes everything else: an actor restarted from a known-good state usually
succeeds, because its failures are transient; a fill asked again with the same tree, the
same oracle and the same evidence usually returns the same wrong body. So a restart is
admitted **only when its inputs have changed**, and a restart that would replay the last
attempt is not taken.

| Scope | Detect | Restart | Escalate |
|---|---|---|---|
| unit (one fn, impl or file) | the fix loop's error-set signature; `domain/unit_clock.py` (`UNIT_BOUND`) | the fix loop's own retries; a retrospective written at give-up (`adapters/retros.py`) changes the unit's evidence fingerprint (`domain/evidence_premise.py`), which lifts its exclusion | `fully_exhausted` / the retrospective gate: the unit leaves dispatch |
| fill (one schedule of routes) | `domain/fill_asks.py`: a run whose every target was asked under unmoved evidence | deferral to the next fill, which is re-planned from the sweep; a keep that moved the premise ends the fill early | the fill's wall clock (`FILL_PHASE_STOP`) |
| tree (the sources) | the batch ratchet; the crash localiser and settler | the ratchet restores an older tree; heal restores `.best_tree`; the crash/layout retrigger re-stubs a component | the retrigger's attempt cap |
| loop (the iterations) | per red test: `due_reading` over the ask ledger; the zero-payoff shadow (`domain/zero_payoff.py`), `flat_score.py`, the stall counter | **the ask ledger**: a red test's units are offered again only under a new `ask_fingerprint`; `drop_standing_flat` (`domain/dispatch_batch.py`) removes a unit whose file, retrospective prompt fields and crash excerpt are unchanged (`evidence_fingerprints`); `asked_unchanged` (`domain/dispatch_rank.py`) removes a served unit nothing owes that was already opened under its own body and its red tests' words; `TDD_ESCALATE_AFTER` raises an owed ask to the maximum reasoning level | `cluster_failed` closes the loop red with the red ledger; the run stops: `NO_VERDICT_STOP`, `GPU_GUARD_STOP`, plateau exit, `NO_FRONTIER_STOP`, `UNSEEN_STOP`, `LLM_CUT_STOP` |
| process | `lib/record_death.sh` is the pipeline's parent and records every exit in `logs/deaths.jsonl`; the flow auditor (`pipeline_core/audit.py`, checks C1–C14, every 15 minutes with `--audit`) reports invariant violations | none, deliberately: an unattended relaunch spends GPU nobody decided to spend | the operator |

What the record says about this table:

- **Restart must change the inputs.** Run `837207-ce5b1647` (2026-09-15) kept three
  compiling, non-paying bodies, and the units that wrote them were then excluded for
  three hours on a frontier of three. Nothing could tell a re-ask with the same evidence
  from a re-ask with new evidence. The evidence fingerprint is that distinction.
- **A restart that destroys progress has to tell every gate.** In run `259637-36db9aad`
  (2026-08-07) the layout retrigger deliberately reverted 56 of 178 bodies. The
  re-admission gate kept comparing against the high-water mark taken before the revert,
  so the loop never re-admitted a unit again: nine hours, 148 fills, +5 tests.
  `domain/destruction_epoch.py` now tells the gates that read that number.
- **The zero-payoff shadow is a detector without a stop.** Over every recorded run it
  fired 46 times in 28 runs: 26 vindicated, 6 false alarms, 14 unresolved, and 13
  authoritative stops it missed (`python scripts/zp_shadow_calibration.py`). A stop must
  never fire on a climb that was about to pay.
- **The end-game is where the loop deviates.** In 43 runs, 106 of the 128 iterations that
  entered with 15 or fewer failing tests moved no verdict; only 32 of the 724 fills
  inside them paid, the dominant classes being a wrong body (315) and a verdict the body
  could not reach (216). Run `1680589-7a6824f8` sat at 158 for four sweeps and then went
  green, so escalation is judged per red test, never by a cluster-wide flat counter.

### Why there is no loop supervisor

A supervisor that restarts the loop, with a retrospective phase on each restart, was
reviewed on 2026-11-03 and not built. Restarting the loop on the same tree, oracle and
stores replays the iteration; a restart that also reverts bodies is the lock
`destruction_epoch` exists for. Everything a restart could legitimately do is already
done per red test by the ask ledger above, and the loop already stops when nothing new
can be asked (`cluster_failed`). There is no "next cluster" to free up: task 2a runs one
loop over one tree, and every world is summed into the same sweep. A retrospective does
not supply a missing fact either: the session builder, which the schedule opens first,
already carries the panic, the observed value, whether the failing tests enter each
editable unit, the crash and the compiler note, and has no slot for retrospective prose.

The one decision still open is a cap that would withdraw a red test after a fixed number
of changed fingerprints while the ask ledger would still offer the next one. It is not
shipped: on run `1680589-7a6824f8` the tree sat flat for four sweeps and then went green,
and a cap of three would have closed it red. It will be considered only if a census shows
a measured fact the builders do not already render, which a later attempt carrying it
moved.

---

## Stops, budgets and bounds

The loop has several ways to stop a run that is spending GPU on fills that change no
verdict. Each one is a **measurement boundary**: a stop that fires before the reading a
run was launched to take does not save the GPU, it converts every batch already spent
into nothing. Before launching a run to validate a mechanism, name the iteration its
reading arrives at and check every stop against it. All of these are read once at cluster
entry and echoed on the cluster-entry line. Defaults are the code's.

| Knob | Default | What it does | Log / event | Off |
|---|---|---|---|---|
| `TDD_NO_VERDICT_SWEEPS` | 3 | Stops at the N-th judged sweep that landed bodies and moved no verdict (counted from iter 2; fires before that iteration's dispatch). | `NO_VERDICT_STOP` | `0` |
| `TDD_FLAT_SCORE_REPEATS` / `TDD_FLAT_SCORE_WINDOW` | 2 / 5 | Flat-score GPU guard: N identical readings (including the entry sweep) inside the window. `TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1` (default 0) forbids it from firing mid-pass. | `GPU_GUARD_STOP` | `REPEATS < 2` |
| `TDD_MAX_STALLS` | 3 | Plateau exit: charges only on a *completed* flat traversal and closes the cluster on the N-th charge. | `stall=a/b` on the iteration line | `0` disables the exit, not the loop |
| `TDD_NO_FRONTIER_STOP` | 2 | Only while `TDD_MAX_STALLS=0`: the N-th consecutive iteration with nothing dispatchable, after the bounded re-admission (`RETRO_RESET_MAX_ATTEMPTS`, default 2) is spent, stops the run at the iteration boundary. | `NO_FRONTIER_STOP`, `phase_c.tdd.no_frontier_stop` | `0` |
| `TDD_UNSEEN_STOP` | 1 | The N-th consecutive green-build sweep on which *every* owed red test is unseen stops the run before dispatch; it can prevent only a guessed fill. | `HEALTH ‼ UNSEEN_STOP`, `phase_c.tdd.unseen_stop` | `0` |
| `TDD_FILL_PHASE_MAX_MINUTES` | 0 | Wall-clock bound on one fill phase; fires at the next unit boundary. Must be `0` or at least the fill budget plus the longest healthy unit. | `FILL_PHASE_STOP`, `phase_c.tdd.fill_phase_stop` | `0` |
| `LLM_CUT_STOP_SHARE` / `LLM_CUT_STOP_WINDOW` | 4 / 100 | Run-level cut stop (`domain/reasoning_cap.py::CutRate`): fires when the cut calls among the last WINDOW reach SHARE % of a full window (4 % of 100 = 4 calls). See below for what "cut" means. | `HEALTH ‼ LLM_CUT_STOP`, `llm.cut_rate_stop` | `SHARE=0` |
| `TDD_FILL_BUDGET_MINUTES` | 0 | Not a stop: the minutes one fill's route runs share by weight (never-served and paying runs 2, served 1, re-buys 0 and deferred). | `[phase-c/schedule] fill N:` | `0` = order only |
| `SLICE_SESSION_MAX_PER_FILL` | 0 (unbounded) | Not a stop: how many slice sessions one fill opens; the rest are deferred to the next fill. | `DEFERRED … kind_cap` | `0` |
| `SLICE_SESSION_MIN_TURNS` | 3 | Not a stop: a session's share is floored at this many turns at its measured turn cost. | `floor NxMm` | `0` |
| `SLICE_SESSION_MAX_MINUTES` | 90 | Per-session wall clock. | | |
| `FILL_UNIT_SHARE_MULTIPLE` | 4 | Not a stop: one claimed unit may take this many times its run's share, then ends at its next fix-loop attempt boundary. Inert without a fill budget. | `UNIT_BOUND`, `phase_c.unit.bound` | `0` |
| `PHASE_C_REPLAN_ON_PREMISE` | 1 | Not a stop: a fill ends at the first claim after a keep that moved which tests fail, and the next sweep re-plans. | `premise moved after …` on the schedule line | `0` (frozen plan) |
| `PHASE_C_TEST_ISOLATION` | 1 | Measurement setting: an aborted test binary is re-run one test per process. | | `0` |
| `PHASE_C_FILL_SCHEDULE` | 1 | The fill schedule; `0` restores the fixed pre-schedule walk for an A/B. | | `0` |
| `PHASE_C_TEST_TIMEOUT` | 120 s | Bound on every subprocess that executes the reconstructed tree. | | |
| `PHASE_C_TDD_BACKSTOP` | 10000 | Iteration backstop: the one bound that should remain when the others are disabled. | | |

**What counts as a cut call.** A call is cut when no decided answer reached the record
(`reasoning_cap.answer_decided`, commit `5e88c2e`). Without a followup, the main stream
must have content and must not have ended on `length` or the think cap. When the
controller forced a followup (the main reply was empty or budget-cut), the *followup's*
row decides: it must have content and must not have ended on `length`, the think cap or
an error. `LLMController.run` stamps the verdict on the `llm.stream_meta` row as
`decided`. The previous rule counted every forced followup as a cut: on run
`2833915-23e0c633` (2026-11-09) four calls ended their main stream inside the reasoning
with the decision written there, all four followups wrote the reply the pipeline then
served, and the stop still fired at call 376 of 424. Replayed over 347 recorded runs, 337
of the 1,941 calls the old rule counted as cut had an answered followup, and the stop
fires on 60 runs instead of 88 (`python scripts/cut_stop_census.py`, `--legacy` for the
old rule). The health line and the stop print `followup-answered=N`. Notes-extractor
calls are not counted.

The arithmetic for sizing each stop against the reading a run needs, and the run ids
behind each default, are in [`AGENTS.md`](AGENTS.md) ("A stop knob set to 'spare GPU' is
the most expensive setting in `.env`").

---

## Launch, resume, restart (anyhow)

The reconstruction lives in `tmp/anyhow`. `continue.sh` is the only launcher for that
tree. It checks the pre-run stamp, refuses a second pipeline, runs the model conformance
check (served id, reasoning dialect, parser; report in `logs/model_conformance.json`),
starts the process under the death record, and must be passed `--no-tail` so closing the
terminal does not SIGTERM the run (run `929966-abe13d11`, 2026-08-29, exit 143 after 54
minutes). Do not wrap a launch in `timeout`, and do not background `continue.sh` with `&`
instead of `--no-tail`: both signal the whole process group. The run log is
`logs/pipeline_anyhow.log` (append-only; the pipeline rolls it to `.1`, `.2`, …).

Every launch through `continue.sh` (including `restart_from.sh` and `redo.sh`) goes through
the stamp: if the pipeline code changed since the last gate, `continue.sh` exits 3 until
`pre_run.sh` has run.

### Gate

```bash
bash pre_run.sh                 # the four stages below, then write the stamp
bash full_pre_run.sh            # pre_run.sh, then the whole integration suite; removes the stamp on failure
bash pre_run.sh --walks-only    # walks only; writes no stamp (for a fix loop, not a launch)
pytest -m walk                  # the walks alone, outside the gate
bash scripts/run_integration.sh # every integration test, cargo-backed ones included (-k … goes to pytest)
```

`pre_run.sh` runs four stages, in this order because each is unreadable while the one
before it is red:

1. **Extractor build** — `cargo build --release` in `tools/skeleton_extractor`. The stamp
   hashes the extractor's Rust sources, but the pipeline executes the compiled binary;
   only a build keeps the two from drifting. Run `955468-7b947bd0` (2026-08-29) executed a
   four-day-old binary that lacked a record field, so a shipped mechanism was inert while
   every identity read as current.
2. **`check.sh`** — is the code correct ([Development](#development)).
3. **Gate contracts** — `INTEGRATION=1 pytest -m gate tests/integration`: does each
   decision still see what real cargo reports. A decision's inputs are derived from a real
   capture through the real runner; zero collected also fails.
4. **Walks** — `pytest -m walk`: does the loop's decision sequence go where the theory
   says. A walk replays a recorded pathological run through the real `run_phase_c_tdd`
   with cargo and the model stubbed, and asserts that a decision became *reachable*
   (never a score). Every walk carries its negative control: the same walk under the
   pre-fix behaviour must reproduce the bad outcome.

On a full pass `pre_run.sh` writes `.pre_run_stamp`, the content hash of `pipeline.py`,
`pipeline_core`, `tools` and the extractor's Rust sources (`lib/tree_stamp.sh`; tests and
docs are outside it). Git cannot play this role: runs launch from an uncommitted tree.

`full_pre_run.sh` is the slow gate (it needs cargo and the pinned toolchain, no GPU, runs
offline). It exists because `check.sh` skips every cargo-backed integration test, and a
change to how the pipeline measures could turn a decision off with every fast test green:
test isolation (2026-09-19) rebuilt aborted binaries as completed sections, and two
decisions keyed on "did a binary not complete" never fired again until run
`118933-877c43bd` (2026-09-27). It prints `FULL PRE-RUN PASSED` or
`FULL PRE-RUN FAILED at …`. Run it before a launch that validates a scheduler,
measurement or reader change, and after a toolchain, cargo or trybuild update.

A recorded run becomes a walk fixture with
`python tools/distill_walk.py --cluster stable --run <run-id> logs/pipeline_anyhow.log.1 logs/pipeline_anyhow.log -o tests/fixtures/walks/<run-id>.json`,
then a test module under `tests/walks/` driving `tests.walks.harness.replay`.

`continue.sh --no-pre-run …` launches untested and prints that it did. Use it only as the
escape hatch it is.

### Stop without relaunch

```bash
touch /tmp/pipeline_graceful_stop    # exits at the next fill checkpoint
```

`continue.sh --y` and `restart_from.sh` / `redo.sh` stop **then relaunch**. If the ask is
only to stop, touch the sentinel and wait. Prefer the graceful stop to `--force` (SIGTERM
then SIGKILL), which interrupts whatever unit is in flight.

### Resume (keep the tree, keep the fills)

```bash
bash continue.sh --y --no-tail       # graceful stop if one is running, then resume
bash continue.sh --force --no-tail   # SIGTERM then SIGKILL, then resume
bash continue.sh --no-tail           # resume if nothing is running; refuses to kill
```

Resumes at the first phase `pipeline_state.md` does not mark done. Fills in
`.skeleton-filled/` stay. A healthy vLLM container is left alone. It validates only what
runs *after* the resume point: a run that died in task 3 resumes in task 3.

Other `continue.sh` flags:

| Flag | Effect |
|---|---|
| `--keep-augmented` | `git reset --hard` to `task0c-augmented-*`. Generated tests stay. Strip, bake, t1.5, task1 and task 2a re-run on a stub tree. Mutually exclusive with `--restart-from-pre-augment`. |
| `--restart-from-pre-augment` | Reset to the restore point before augmentation so task0c itself re-runs. Keeps the task0d harvest when the `task0d-done-*` tag exists. |
| `--no-pre-run` | Skip the stamp check. |
| `--wipe` | Removed. Refuses (exit 2): it discarded the run log and the reconstruction, which are the evidence a run is diagnosed from. To rebuild from upstream deliberately, run `pipeline.py --repo <URL> --clean` yourself. |

`resume_from_t2.sh` is a shim for `restart_from.sh task2`, so it drops the same products.
`wipe.sh` is not a wipe: it forwards to the removed `--wipe` flag and exits 2.

### Re-enter a phase

```bash
bash restart_from.sh <stage> [--force] [--keep-fills]
```

`--force` SIGKILLs a running pipeline; without it the stop is graceful. It always
relaunches detached (`--no-tail`). The named stage and everything after it are cleared in
`pipeline_state.md` (`lib/restart_from_lib.sh`, shared with the smoke launchers), and the
`passing-*` lock tags are deleted. Short labels (`t1`, `t2a`, `t3`, …) are accepted.

`task2` and `task2a` also remove what that phase wrote: `.skeleton-filled/`, `.best_tree/`,
`.skeleton/_tdd_floor_*.json`, `.phase_c_logs/retrospectives.json`, the ask ledgers and
the red ledgers. The seed copies a skeleton file only when the destination is missing, and
heal would copy the best tree back, so both trees have to go or the first sweep scores the
old fill. The layout, the generated tests and the FRs stay. `--keep-fills` skips that
removal and re-enters on the bodies already on disk — the lever for a loop-behaviour fix
when task 2a is already marked done.

| Stage | What re-runs | What is kept |
|---|---|---|
| `task0d` | the behaviour harvest, augmentation and everything after (`--reharvest-behaviours`: reset to `task0c-pre-*`, delete `task0d-done-*`) | task0a/0b |
| `task0c` | augmentation and everything after (`--restart-from-pre-augment`) | task0a/0b and the task0d harvest |
| `task1` | FR synthesis and the whole reconstruct loop | t0c tests, layout, fills (task 2a still sees them) |
| `task2` / `task2a` | the one-tree fill from stubs → publish → test → genesis | t0c tests, the t1.5 layout (`.skeleton/`). Fills, best tree, floor, convictions, ask and red ledgers are removed unless `--keep-fills` |
| `task2b` / `task2c` | publish onward (skips the fill) | the filled tree |
| `task3` / `task4` | test / genesis only | all fills |

task0a/task0b and earlier need `redo.sh` or a re-clone. The **body ledger**
(`logs/body_ledger/<project>/`) lives outside the workspace and survives every launcher:
cluster entry may splice prior proven or unmeasured bodies back onto fresh stubs, and the
cluster-entry line says how many. That is by design; delete it only for a crate that has
never been tried.

### Rebuild the layout

```bash
bash redo.sh              # graceful stop, wipe `.skeleton-filled/`, restore the post-t0c tag
bash redo.sh --force      # same, SIGKILL instead of graceful
bash redo.sh --full --force
```

`redo.sh` always launches `continue.sh --y --keep-augmented --no-tail`. That **keeps
task0c** (generated tests come back from the tag; sentinels skip augmentation). It does
**not** keep the current t1.5 layout: `.skeleton/`, `.skeleton-filled/` and `.best_tree/`
are archived off the workspace, strip is cleared, and bake + t1.5 author a new layout
before task 2a. `--full` also deletes `.skeleton/` in `redo.sh` itself and is what re-arms
the bake/pin harvest. The underlying `--restart-from-augmented` is a `git reset --hard` to
a tag that predates every fill, so it deletes the tracked tree directories; a past tree is
then reachable only by sha (`git archive <sha> <paths> | tar -x -C <scratch>`).

### Which launcher for which question

| Question | Command |
|---|---|
| Resume a stopped run on the same code | `bash continue.sh --y --no-tail` |
| A loop fix (dispatch, attribution, keep/revert, schedule, sessions) on the existing filled tree | `bash pre_run.sh` then `bash continue.sh --y --no-tail`, or `bash restart_from.sh task2a --force --keep-fills` when task 2a is already marked done |
| A loop fix read from stubs, same layout and tests | `bash pre_run.sh` then `bash restart_from.sh task2a` |
| A test-phase fix (task 3, the end gate) | `bash pre_run.sh` then `bash restart_from.sh task3` |
| A construction fix (t1.5 layout, bake, strip, FR synthesis) | `bash pre_run.sh` then `bash redo.sh --full --force` |
| A test generator or admission change | `bash pre_run.sh` then `bash restart_from.sh task0c --force` |
| A behaviour-harvest change | `bash pre_run.sh` then `bash restart_from.sh task0d --force` |
| Stop only | `touch /tmp/pipeline_graceful_stop` |

Validate a fix with the cheapest lever that executes the changed phase on a tree that can
show the reading; a from-scratch run costs hours of GPU before the phase under test even
starts. Do not `redo.sh` to validate a loop-behaviour fix: t1.5 re-authors, and the next
score describes a different scaffold.

---

## Smoke crates and model comparisons

Bundled crates in `tests/fixtures/` exercise the whole pipeline in minutes to hours on a
real model. All need vLLM already serving on `:8000` and never touch `tmp/anyhow`.

```bash
bash run_microcrate.sh               # FULL: rebuild the extractor, fresh tmp/microcrate, from scratch
bash run_microcrate.sh --from=task3  # FAST: keep the bake and fills, clear phases from <stage>, resume

bash run_hardcrate.sh                       # L3 recovery smoke, all trap levels, from scratch
bash run_hardcrate.sh --complexity=N        # assemble a crate with traps of level <= N (0 = baseline)
bash run_hardcrate.sh --from=t2a            # resume tmp/hardcrate from a stage, keeping the fills
bash run_hardcrate.sh --from=t2a --drop-fills  # resume on stubs (see below)

bash run_hardcrate_multi.sh          # scale + real nightly cluster smoke
bash run_smokes.sh ["note"]          # microcrate + hardcrate --complexity=7 from scratch -> logs/perf_ledger.jsonl
```

- **`run_microcrate.sh`** — the happy-path smoke. `--from=<stage>` accepts
  `task1 | task2 | task2a | task2b | task2c | task3 | task4`.
- **`run_hardcrate.sh`** — the hard-on-purpose crate. It measures how much of the
  failure-recovery loop the pipeline can drive: the flow-audit recovery invariants
  C1/C2/C3/C4/C6 should pass. It is non-gating by design; the only hard failure is a
  regression against `.hardcrate_ratchet.json`. Without `--from` it re-assembles the crate
  and rebuilds `tmp/hardcrate`; with `--from=STAGE` (`t1|t2|t2a|t2b|t2c|t3|t4`) it only
  clears phase entries. `--drop-fills` (with `--from=t2` or `--from=t2a` only) first runs
  the same product drop as `restart_from.sh task2a`, and **moves
  `logs/body_ledger/hardcrate` aside** (renamed with a timestamp, never deleted), so the
  cluster-entry re-seed cannot put proven bodies back and the fill really starts on stubs.
  Without it, a resume of a tree the last run left green closes task 2a on its first
  sweep (run `3669371-9d5a7361`, 2026-11-06: 168/168).
- **`run_hardcrate_multi.sh`** — every fn intentionally easy; the point is enough fns and
  files for `PIPELINE_PARALLELISM > 1` to have real worker contention, plus a real
  nightly-toolchain cluster (`core/build.rs` mirrors a real feature probe). It runs
  `hcm_core` alone; the cross-crate half needs Cargo `[workspace]` support the pipeline
  does not have yet.
- **`run_smokes.sh`** — ensures vLLM serves the `MODEL` named in `.env` (a healthy
  container is left alone; otherwise `docker_vllm.sh --no-pull --y`), runs the model
  conformance check, runs both smokes from scratch, and appends one row per run to
  `logs/perf_ledger.jsonl` via `tools/perf_ledger.py`. Run it before touching `.env`; the
  row records it.

### Comparing two models or two reasoning levels

```bash
bash compare_models.sh snapshot <name>          # freeze today's tree as the seed
bash compare_models.sh arm <name> <qwen|muse>   # restore the seed, point .env, restart vLLM
bash compare_models.sh report <A=run> <B=run>   # side-by-side trajectories
bash compare_models.sh list                     # seeds on disk + run ids in the sink
```

`arm` restores `.skeleton` and `.skeleton-filled` together (they are a pair), so both arms
start from the same tree and the same layout. What is compared is progress per sweep and
per hour, never tokens per second (`tools/compare_trajectories.py`): a model whose
iterations are slower but whose fills land reaches a pass count in fewer iterations. Both
arms run at the same context window (`COMPARE_CTX`, default 32768) so the result does not
measure context. A `redo.sh` between arms replaces the seed and ends the comparison. Note
that the `qwen` arm still points at `Qwen/Qwen3.6-27B-FP8`.

To compare two **reasoning levels** on runs already recorded, use
`python scripts/effort_score_rate.py RUN_ID [RUN_ID ...] [--events logs/events.jsonl] [--cluster stable] [--curve]`.
It reads `events.jsonl` offline and prints, per fill: wall minutes, calls, stream minutes,
completion tokens, pass before → after, tests per wall hour, per stream hour and per 100k
completion tokens; `--curve` adds every keep reading on a time axis. The decision rule in
its header: "the maximum level scores faster" holds only if, from the same entry tree
(same `tree_sha`, oracle and code), the maximum run's tests per wall hour is higher.

---

## Serving the model

Self-hosted only — the pipeline never calls a cloud LLM. Three transports are implemented
(`pipeline_core/adapters/llm_transports.py`): **vLLM** (the one in use), **Ollama** and
**SGLang**; `BACKEND=` picks one. Every call goes through `LLMController`, which decides
the call's reasoning level, stamps it on the call's events, and owns the followup when a
main reply comes back empty or budget-cut.

### The one switch

`MODEL=` is the only line you change to move the whole stack between models. `TASK*_MODEL`
fall back to it, `VLLM_MODEL_NAME` defaults to it on both the Python and the launcher side,
and the model's **profile** (`pipeline_core/domain/model_profile.py`) supplies the request
dialect, prompt tier, route order, server image and parsers, context window, sampling and
reasoning calibration — read by the pipeline *and* by `docker_vllm.sh` (through
`scripts/model_profile_shell.py`). An id no profile matches is refused at launch rather
than defaulted, because the default dialect is silently wrong for any model that is not
shaped like it. The run log's `model profile:` line names the family and where each value
came from (`env`, `profile` or `default`).

| | `qwen3` profile | `muse-glimmer` profile |
|---|---|---|
| Ids | starting `qwen3` (e.g. `Qwen/Qwen3.8-27B-FP8`, the model in use since 2026-11) | starting `muse-glimmer` (`RedHatAI/Muse-Glimmer-30B-FP8-block`) |
| Reasoning dialect | `enable_thinking` — a boolean the chat template branches on, so `false` is a real off; the level is sent as `reasoning_effort` beside it | `reasoning_strength` — a rung (`low`/`medium`/`high`/`xhigh`) rendered into the system prompt; there is no off, so "off" is `low` |
| Level words (medium / max) | `medium` / `xhigh` | `medium` / `high` (`xhigh` is pending: uncalibrated on this hardware) |
| Server image | `vllm/vllm-openai:latest` | `vllm/vllm-openai:muse-glimmer` (vLLM 0.27+) |
| Parsers | `qwen3` reasoning, no tool-call parser | `muse_glimmer` reasoning and tool-call |
| Context window | 131072 (ceiling from Qwen3.6-27B-FP8's measured 136,533-token KV pool at TP=2, 2026-08-11) | 131072 (189,405-token KV pool at TP=2, 2026-08-11) |
| Thinking reserve | 50,176 tokens (measured on Qwen3.8 at `xhigh`, 2026-11-06) | 32,768 tokens (2026-09-21, at `high`) |
| Runaway reasoning cap | 335,000 chars (the measured healthy maximum + 25 %, 2026-11-06) | none (re-asked uncapped, the cut prompts finished naturally) |
| Repetition penalty | 1.0 (the Qwen3.8 model card) | 1.05 (what every Muse run here sent; the card recommends none) |
| Route order | default | file-primary (halved hardcrate calls, 2026-08-12) |

Every calibrated value in a profile names the run or measurement it came from in its
`calibration` field, so a measured value can be told from an inherited one.
`VLLM_REASONING_EFFORT` and `VLLM_REASONING_STRENGTH` are **retired**: a line naming either
is ignored and named at startup.

### Reasoning levels

Until 2026-11-07 every call ran at one process-wide maximum. On run `336559-eb0222a5` the
behaviour book's prediction calls used 2.7× Muse's completion tokens per call at the same
decode rate, so the 16.7 h of task0d was reasoning length. The level is now decided once
per call (`pipeline_core/effort.py`, rule in `domain/effort_policy.py`), from three inputs:

- **`LLM_EFFORT`** — the default level, `medium` or `max` (unset: `medium`);
- **`LLM_EFFORT_PHASES`** — `phase=level` pairs, comma separated; the innermost configured
  phase on a call's path wins (`t0d.book=max` beats `t0d=medium`). Phase ids are
  `domain/phase_registry.py`'s; an unknown one refuses the launch;
- **escalation**, which wins over both: **`TDD_ESCALATE_AFTER`** (default 12) is the number
  of owed asks below max (landings and planned misses, any function, any fingerprint,
  since the test was last green) after which a red test owes its functions at max, once
  per function per red episode. A tier walk spent below max is walked once more at max
  whatever the count. `0` turns escalation off. It is not a stop: it raises the level of
  asks that are already owed.

The cluster-entry line confirms all three (`effort=…, escalate-after=N`).
`VLLM_EFFORT_WORD_MEDIUM` / `VLLM_EFFORT_WORD_MAX` override the word a level is sent as,
for calibration only (`scripts/measure_runaway_cap.py`). The same level name on two
templates is not the same amount of reasoning; an A/B is what says how far apart they are.

### Why vLLM, on two GPUs

A 27–30B checkpoint at FP8 does not fit one 24 GB card with a 131k-token window, so the
server shards it: `--tensor-parallel-size 2` (`VLLM_TENSOR_PARALLEL=2`),
`NCCL_P2P_DISABLE=1`, `--gpu-memory-utilization 0.97`. `docker_vllm.sh` refuses to launch
a window longer than the profile's measured ceiling, so a forgotten edit is an error at
launch instead of a KV-allocation OOM later.

**That sharding is what buys the parallelism.** `PIPELINE_PARALLELISM=4` is the single
global concurrency knob — the task0c split-and-merge degree and the phase-C fill workers —
and it is matched to the server's `--max-num-seqs 4`. Four fills are in flight as one
continuous batch on the two cards; splice and `cargo` stay serialised per source tree.
`--enable-prefix-caching` is what makes it pay: the fill prompts share a long common
prefix, so the batch reuses it instead of re-attending it four times.

Ollama has no equivalent here: no tensor-parallel shard of an FP8 checkpoint across the
two cards, and no batch for `PIPELINE_PARALLELISM` to match. It stays as the low-setup path
for a small model on one card; keep `PIPELINE_PARALLELISM=1` on it.

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
tag it already holds — this box once served a three-month-old vLLM that way, and a model
needing a newer engine fails as "unknown architecture" rather than "your image is stale".
The corollary: **restarting the server also upgrades it**, so leave a healthy container
alone in the middle of a measurement you intend to compare against an earlier run. The
container id is kept in `/tmp/vllm_container.id`.

### The Ollama and SGLang paths

```bash
BACKEND=ollama
MODEL=qwen2.5-coder:7b
OLLAMA_NUM_CTX=32768          # the transport's own default is only 8192
PIPELINE_PARALLELISM=1
```

Install Ollama, then `ollama pull qwen2.5-coder:7b`. Ollama and SGLang model names have no
profile; their capabilities are in `MODEL_CAPS` (`llm_transports.py`): `think` (reasons by
default) and `native_ollama` (use `/api/chat` instead of the OpenAI-compatible endpoint —
`qwen3.6:27b` needs it, the compat layer returns an empty string whenever thinking is on).
`ModelfileHf` builds a local Ollama model from a Qwen3.6-27B Q8_0 GGUF, and
`ModelfileSGLang` is the SGLang server config for the same model.

### Models this pipeline has been run on

| Model | Backend | Notes |
|---|---|---|
| `Qwen/Qwen3.8-27B-FP8` | vLLM, TP=2 | In use since 2026-11; the model of the [reference run](#status). |
| `RedHatAI/Muse-Glimmer-30B-FP8-block` | vLLM, TP=2 | The default from 2026-08-12 until the move to Qwen3.8. |
| `Qwen/Qwen3.6-27B-FP8` | vLLM, TP=2 | The arm Muse was compared against (below). |
| `mattbucci/Qwen3.6-27B-AWQ` | vLLM | Smaller-footprint quantisation of the same model. |
| `qwen3.6:27b` | Ollama (native endpoint) | Needs `/api/chat`. |
| `qwen3.6-27b-q8` | SGLang | The SGLang served name uses a dash, not a colon. |
| `qwen2.5-coder:7b` | Ollama | A first-run model, too small for a real reconstruction. |

**History: Muse Glimmer vs Qwen3.6 (2026-08-12).** The A/B matrix over identical crates and
seeds put output-contract compliance at 96–100 % for Muse Glimmer against 41–58 % for
Qwen3.6 — Qwen3.6 ignored the FN/FILE output contract about half the time — and Muse
completed the microcrate and hardcrate smokes at equal or better scores. Both sat at the
same 83-of-106 plateau on a cold `anyhow` tree at the time, so that plateau was a pipeline
property, not a model ceiling. See `QwenVSMuse.txt`. This comparison predates Qwen3.8 and
the per-call reasoning levels.

---

## Monitoring a run

```bash
.venv/bin/python monitor.py            # the terminal monitor
.venv/bin/python monitor.py --demo=12  # cycle views every 12 s
tail -f logs/pipeline_anyhow.log       # the raw run log
```

`monitor.py` renders the event stream (`logs/events.jsonl`) and the run log. Views: `1`
monitor, `2` pipeline, `3` stream (the live model output), `4` events, `l` the phase
legend, `h` health, `e` phases, `f` the field glossary (meanings of every log field, from
`pipeline_core/monitor/field_glossary.py`). Actions: `c` continue (relaunch), `g` graceful
stop, `s` stop, `p` power off the machine when the run finishes, `a` auto mode. **Known
issue:** the `w` (wipe) action still calls `wipe.sh`, which refuses; use the launchers above
instead.

What a run leaves behind, all under `logs/`:

| Path | What |
|---|---|
| `pipeline_anyhow.log` (+ `.1`, `.2`, …) | the run log, one `[phase-c/tdd] iter N:` line per iteration |
| `events.jsonl` | every structured event (`llm.*`, `phase_c.*`, `flow_audit.report`, …) |
| `prompts/`, `thinking/` | every prompt and response by sha, and the model's reasoning by response sha — join them to audit exactly what context a fill got |
| `deaths.jsonl`, `births.jsonl` | every process start and exit, with status, signal and log tail |
| `body_ledger/<project>/` | proven and unmeasured bodies, by tree and test outcome |
| `route_ledger/` | per-route outcomes the fill schedule reads |
| `perf_ledger.jsonl` | one row per smoke run |
| `model_conformance.json` | the launch conformance report |

Offline readings over those records are in `scripts/`. The ones to know first:
`python scripts/pattern_census.py` (the recurring pathology classes across every recorded
run — run it before diagnosing a stall as new), `scripts/fill_time_split.py` (per-fill
time), `scripts/cut_stop_census.py`, `scripts/effort_score_rate.py`, and
`scripts/zp_shadow_calibration.py`.

`analysis.ipynb` is an older Jupyter notebook (last changed 2026-05-16) for ad-hoc
inspection of the SQLite knowledge store (`tmp/<project>/pipeline_knowledge.sqlite`) and the
log. It predates the event stream and the monitor; prefer `monitor.py` and the scripts.

---

## model_interact.py

A standalone wrapper to send a prompt to the configured model:

```bash
python model_interact.py "your prompt here"
python model_interact.py --file prompt.txt
echo "prompt" | python model_interact.py
python model_interact.py "prompt" --output response.txt
python model_interact.py "prompt" --system "system prompt"
```

---

# Deterministic Process

The goal is a universal set of rules — a methodology for describing software to an AI so
that it can rebuild it completely and correctly from scratch, deterministically. A product
repository would then consist of four artefacts:

1. `README.md`
2. business requirements
3. functional requirements
4. automated tests

and no source code. The pipeline builds the product, the tests pass every time, and the
product may differ in small ways but is behaviourally equivalent.

The methodology is captured in `genesis.md`. It starts empty and is refined iteratively, by
"training" on real, complex repositories with good test coverage. Each iteration answers one
question: given only the tests of a project, what reasoning process should an AI follow to
produce an implementation that passes them?

The training works by:

1. taking a real, complex project, growing its test suite while the original still exists
   to test against (task0d, task0c), and capturing its skeleton (bake);
2. removing every function body, leaving only tests and signatures (strip);
3. letting the AI reconstruct the bodies from the tests, the skeleton and its own
   functional requirements (task1, task2a);
4. running the tests — they fail, because the reconstruction is imperfect (task3);
5. having the AI study the failures and turn them into general rules in `genesis.md`
   (task4);
6. repeating until every test passes.

Each failure should teach a general lesson — not "I missed function X" but "I was not
reading this class of signal from tests". Over many iterations `genesis.md` should become a
mature, project-independent methodology. The hypothesis is that, once mature, it works on
any project in any language on the first or second attempt. The experiment ends when all
tests pass, but the real output is `genesis.md`, not the reconstructed code.

Two rules keep the experiment honest:

- **Generic mechanisms only.** No pipeline rule names a crate, file, test, trait or symbol,
  and the pipeline never special-cases a language construct the model is the expert on. A
  fix that works only for the instance in front of you does not count.
- **The crate is the measurement.** Nobody edits the crate under reconstruction by hand —
  not a body, not a test, not one obvious line. Every pass count describes a tree the
  pipeline produced.

---

## License

Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Copyright 2026 Virgilio Lino (Humanetes).
