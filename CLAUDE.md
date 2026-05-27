See autopilot.md for autopilot behavior rules.

Past session handovers: see handover/HANDOVERS.md.

Do not use RemoteTrigger, ScheduleWakeup, or create remote schedule routines. No code or data should be sent to Anthropic's cloud servers via scheduled agents.

IMPORTANT: Always produce compact structured output instead of markdown prose when generating guides, strategy docs, or rule sets for LLM consumption. Use tables, symbolic notation, and abbreviated rules rather than verbose English paragraphs. Prose is an inefficient token format for conveying structured rules to an LLM.

IMPORTANT: Keep all responses short, condensed, and unformatted. No markdown headers, no bullet lists, no tables in prose responses. Plain short sentences only. Code blocks are fine.

---

# Engineering principles

## Never install dependencies manually
Every new tool, binary, or Python package must be added to `setup.sh` (and `requirements.txt` for Python deps). Never run `pip install`, `cargo install`, or `rustup component add` directly — those are one-off fixes that break reproducibility. The correct action is always: add to setup, then run setup.

## Pipeline restart timestamps
Each restart is logged as `PIPELINE STARTED — <timestamp> UTC` in `logs/pipeline_anyhow.log`. Check this to confirm which code version is running.

## Static analysis after every Python edit
After modifying any `.py` file, run `bash check.sh` and fix **all** reported errors — including pre-existing ones unrelated to your change. Never leave check.sh failing.

## Fix processes, not symptoms
Do not patch individual failures in isolation. Ask: what process produced this failure, and how should that process be improved so the failure cannot recur? Examples:
- A type error in pipeline.py → add static analysis to the dev workflow, not just fix that one return statement.
- A coverage tool giving wrong results → improve the coverage backend selection process, not just ignore that file.
- An LLM returning bad output → improve the prompt or retry logic, not manually correct the output.

## Let the LLM fix code, not the autopilot
The autopilot (this assistant) must not directly fix bugs in project source files inside `tmp/`. Those are owned by the pipeline. The correct action is to improve pipeline.py so the LLM produces better output on the next run. The autopilot fixes the process; the process fixes the code.

## Same principle applies to the pipeline itself
If pipeline.py has a bug, fix the pipeline code and restart — do not manually edit workspace files or call pipeline functions directly to work around the bug.

## The model authors its own anti-hallucination notes
When the LLM gets a fact wrong (wrong type, invented method, format-string mismatch, etc.) and recovers via the fix-loop, the LESSON belongs in a place the model itself can read on its next attempt. The model writes the note in its own voice ("I tried X; X failed because Y; the correction is Z"). Humans do NOT curate these notes.

Storage: `functional_requirements_{crate}.md`, section `## Model Notes — Confusions to Avoid`. Append-only within a run, preserved by TASK 1 when it regenerates the rest of FR. Same lifecycle as FR — wiping FR also wipes notes (consistent semantics for "fresh start on this crate").

When read: included in every per-fn prompt (initial + fix) before the file context, so the model loads its own past disambiguations first. Filtered by relevance (file name, symbols) and capped by char budget so the section never bloats the prompt.

When written: after recovered fix-loops (success at attempt > 1) and exhausted fix-loops (cap reached). These are high-signal moments. First-try successes and protocol failures (empty response, splice rejected) don't trigger extraction.

Why one document per crate (FR) instead of a separate notes file: single source of truth. "Everything the LLM needs to know about this crate" is one mental model, one file. TASK 1 just preserves the notes section when regenerating the spec.

---

# Repository structure

The pipeline is its own git repository. Project workspaces live in `tmp/` and are
not committed to the pipeline repo (gitignored). Each workspace is a separate
git repo managed by the pipeline.

```
determ/                     ← pipeline repo (this repo)
  pipeline.py
  model_interact.py
  monitor.py
  continue.sh / restart.sh
  .env                      ← not committed
  tmp/                      ← not committed (gitignored)
    anyhow-master/          ← workspace repo, managed by pipeline
    argh-master/            ← workspace repo
```

---

# Running the pipeline

## Start / restart from scratch

```bash
bash restart.sh --y
```

Kills any running pipeline, cargo-cleans, strips gen_* blocks, starts fresh with `--clean`.

## Resume

```bash
bash continue.sh
```

## Using a git URL or local path (full git-managed mode)

```bash
# Clone from URL into tmp/, checkout pipeline branch
nohup .venv/bin/python pipeline.py --repo https://github.com/dtolnay/anyhow > /tmp/pipeline_anyhow.log 2>&1 &

# Local directory (copies into tmp/, git init if no .git)
nohup .venv/bin/python pipeline.py --repo /path/to/project > /tmp/pipeline_anyhow.log 2>&1 &

# Existing workspace in tmp/ (auto-detects git, backward compat)
nohup .venv/bin/python pipeline.py --project anyhow-master > /tmp/pipeline_anyhow.log 2>&1 &
```

`--project name` resolves to `tmp/{name}` first, then `determ/{name}` (backward compat).

---

# Git workspace system

When `--repo` is used (or `--project` points to a git repo), the pipeline commits
at key transitions. Every step is reversible.

## Branch strategy

```
main/master   ← original source, never touched
pipeline      ← all pipeline commits
```

## Commit points

| Transition | Commit message | Tag |
|---|---|---|
| task0b done | `pipeline: task0b done` | — |
| each cluster task0c done | `pipeline: task0c/stable — anyhow:83.4%` | — |
| before strip | `pipeline: task0c augmentation complete — all unit tests added for {module}` | `task0c-augmented-{module}` |
| after strip | `pipeline: stripped — {module}` | — |
| task1 done | `pipeline: task1 — {module}` | — |
| task3 pass | `pipeline: task3 iter N — {module} tests pass` | `passing-{module}-N` |
| locked | `pipeline: locked — {module}` | — |
| complete | `pipeline: complete — all tests pass iter N` | — |

## Reverting to before strip

```bash
git -C tmp/anyhow checkout task0c-augmented-anyhow
```

## --clean in git mode

Resets to default branch, deletes pipeline branch, recreates it — full clean slate.

---

# Hard rules

**1. Never write project source files directly.**
Never edit files inside `tmp/{project}/src/`. Only pipeline.py via LLM may write those.

**2. Never manually run pipeline steps.**
Never call task functions directly. Start pipeline.py as a process only.

**3. Role separation.**
Autopilot touches: pipeline.py, .env, autopilot.md, shell scripts, CLAUDE.md, handover/.
Pipeline touches: files inside tmp/{project}/.

**4. Never commit secrets.**
.env is gitignored. Never add it to git.

**5. No implementation without explicit approval.**
When asked for evaluation or analysis, discuss findings and proposed fixes until the user says to proceed. Do not implement anything — no edits, no file writes — until the user explicitly says to go ahead.

**6. Ask before committing.**
After implementing a fix, give a short overview of what was done, then ask "May I commit?" Wait for yes or no before running any git command.

**7. Pipeline is self-hosted only — no external LLMs.**
The pipeline must solve every reconstruction issue using only the locally-served model (via vLLM / Ollama / sglang). It may NEVER call out to Claude, GPT-4, Gemini, or any other cloud/external LLM API for any task — not as a fallback, not for "hard groups", not for cleanup. Do not propose, design, or implement any feature whose execution path can reach an external LLM. The only correct response to "the local model is stuck" is to improve the prompt, the architecture, the model itself (fine-tune), or accept the limitation — never to outsource the call. This rule overrides any prior plan items that suggested optional fallback models (e.g. P3.8 in plan.md): such items are permanently rejected, not deferred.

**8. Identify cluster boundaries from source-level compilation directives.**
A "cluster" is the unit by which we split the reconstruction problem into sub-problems. Concretely: identify restrictions in the source (compilation directives like `#[cfg(feature = "...")]`, `#[cfg(nightly)]`, `#[cfg(not(...))]`, cargo features, toolchain selection, `RUSTFLAGS`) and enumerate the distinct restriction sets they imply. The pipeline then iterates restriction-by-restriction; while a given restriction is active, all OTHER restrictions are excluded and the current restriction owns the set of rules in force (which source compiles, which tests run, which env vars apply). Cluster identification is purely static — no LLM. Every task that operates on source or tests is cluster-aware: it sees only what compiles in this cluster, runs only the tests that apply to this cluster, and validates with the cluster's compile/test env. Cluster-derived compile/test parameters (env_extra, features, toolchain) flow through every downstream phase end-to-end — not just at extraction time. Cross-cluster work is the exception, not the default.

**9. Always prefer quality, systemic, deterministic solutions. Never optimise for "faster" or "easier".**
Ranking when picking between approaches: deterministic > systemic > quality. Speed and ease are not criteria. A solution that is 5× slower but reproducible and addresses root cause beats a fast patch that leaves the underlying problem in place. Specifically: (a) prefer extracting structure from source over guessing it via LLM (LLM output is non-deterministic), (b) prefer per-cluster scoped reasoning over global reasoning (smaller scope = less ambiguity), (c) prefer immutable scratch dirs and merge passes over in-place mutation that races, (d) prefer regenerating from upstream truth over caching arbitrary state, (e) prefer documented invariants over implicit assumptions. When in doubt: ASK the user — do not silently pick. When two solutions appear equivalent: pick the one that ADDS MORE CONSTRAINTS — the option that further narrows what the model, the clusters, or the source structures can be. More constraints = less ambiguity = better.

**10. Never hedge a recommendation by inventing difficulty.**
When uncertain about effort or feasibility, do NOT downgrade the more-correct option by labeling it "harder", "more complex", or "more invasive" as a way to give the user balanced choices without committing. That pattern looks thorough but is intellectual laziness — it transfers the decision burden to the user with a thumb on the scale, when what's needed is actual analysis. The two acceptable behaviours are: (a) trace through the implementation enough to make a real recommendation and commit to it, or (b) explicitly say "I haven't analyzed this yet — let me look" and then do the analysis. The unacceptable behaviour is (c) "X might be harder", "Y is more invasive", or any other vague difficulty claim used to manufacture options. If you find yourself reaching for a difficulty label, stop and check whether you actually traced the implementation. If you didn't, either do it now or admit you didn't.

**11. After strip, the pipeline and the LLM have NO access to the original source code.**
The original implementation is read exactly once — during the pre-strip bake step (`_bake_skeleton_artifacts`) — which runs after task0c and before strip. That step extracts every source-derived artefact the pipeline will ever need (skeleton with `unimplemented!()` bodies, fn-name reference counts for leaf-first ordering, cluster manifest, API surface) and saves them to disk. From TASK 1 onwards every phase reads ONLY these saved artefacts plus tests and stripped source; no code path may re-read source files, re-checkout a pre-strip git tag, scan `/tmp/prestrip*`, or otherwise smuggle the original bodies back in. The `task0c-augmented-{module}` git tag exists as a human rollback aid (`git checkout` it manually to inspect), but pipeline code must not read from it. New work that needs source-derived data must extend the bake step, not add a post-strip source read. Violating this rule turns "tests-only reconstruction" into "copy the answer", invalidating the entire experiment.

---

# Improvements enabled by pipeline-in-own-repo

| Capability | How |
|---|---|
| Versioning | Each run records which pipeline commit generated the results |
| Rollback | `git revert` a pipeline commit if a change breaks results |
| CI | Run `pipeline.py --project test-fixture` on push to catch regressions |
| Reproducibility | `git checkout abc1234` pins pipeline version to reproduce past result |
| Diffing | `git diff` between pipeline versions shows exactly what changed coverage |
