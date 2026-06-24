# Architecture — Deterministic AI Reconstruction Pipeline

---

## What This Is

A research pipeline that takes a software project, removes all source code, and
attempts to rebuild it from tests and README alone. The loop runs until all tests
pass. The real output is not the reconstructed code — it is `genesis.md`, a
language-agnostic methodology for translating any test suite into correct functional
requirements.

The hypothesis: once `genesis.md` is mature, applying it to a new project should
produce a passing implementation on the first or second attempt, without iteration.

This document captures the goals, design decisions, known failure modes, and the
planned improvements so that anyone (or any AI session) picking this up can
continue without losing context.

---

## Ultimate Goal

Build a general methodology (`genesis.md`) such that:

    Given: a project's tests + README
    Produce: functional requirements precise enough that any correct implementation passes all tests
    Without: access to the original source code, documentation, or implementation hints

The methodology must be:
- **Language-agnostic** — works for Rust, Python, Go, TypeScript, etc.
- **Project-independent** — no references to specific tests, files, or this experiment
- **Self-contained** — a reader with no prior context can apply it to a new project
- **Composable** — each rule in genesis.md stands alone; adding a new rule doesn't break old ones

The training target is the `agave` project (a Solana validator client in Rust):
large, multi-crate, high complexity, extensive test coverage. A large project
stresses the methodology against a wide range of patterns and produces a richer
genesis.md faster than a toy project would.

---

## Design Principles

The two principles below govern every architectural decision in this pipeline.
They are non-negotiable — they take precedence over engineering convenience,
runtime speed, or simplicity of implementation.

### Principle 1 — Cluster boundaries are extracted from source

A "cluster" is the unit by which the pipeline splits the reconstruction
problem into sub-problems. Concretely: identify the restrictions present
in source code (compilation directives like `#[cfg(feature = "...")]`,
`#[cfg(nightly)]`, `#[cfg(not(...))]`, cargo features, toolchain
selection, `RUSTFLAGS`), and enumerate the distinct restriction sets
they imply. The pipeline then iterates restriction-by-restriction.

While a given restriction is active:
  - All OTHER restrictions are excluded.
  - The current restriction OWNS the rules in force (which source
    compiles, which tests run, which env vars apply).
  - All cluster-derived parameters (env_extra, features, toolchain) flow
    through every downstream phase end-to-end — not just at extraction
    time. Compilation uses them. Test invocation uses them. Logs are
    scoped to them.

Cluster identification is purely static — no LLM. Per-cluster scoping
shrinks the model's cognitive load (fewer cfg branches to consider, less
code to read) and isolates failures to a specific compilation context so
they can be diagnosed precisely. Cross-cluster reasoning is the exception,
not the default.

### Principle 2 — Quality, systemic, deterministic. Never "faster", never "easier"

Ranking when picking between approaches: deterministic > systemic >
quality. Speed and ease are not criteria. A solution that is 5× slower
but reproducible and addresses the root cause beats a fast patch that
leaves the underlying problem in place.

Specific preferences:
  - Extract structure from source over guessing it via LLM (LLM output is
    non-deterministic, source is the ground truth).
  - Per-cluster scoped reasoning over global reasoning (smaller scope =
    less ambiguity).
  - Immutable scratch directories and merge passes over in-place mutation
    (mutation races, merges are reproducible).
  - Regenerate from upstream truth over caching arbitrary state.
  - Documented invariants over implicit assumptions.

When in doubt: ASK the user — do not silently pick.

When two solutions appear equivalent: pick the one that ADDS MORE
CONSTRAINTS. More constraints further narrow what the model, the clusters,
or the source structures can be. More constraints = less ambiguity =
better comprehension.

---

## Determinism Principle

**The goal is not to approximate a solution. The goal is to produce, each time, exactly
the same solution that guarantees all tests pass.**

Every step in the pipeline must move toward deterministic reconstruction:

- **Interfaces must be exact.** Public types, trait impls, function signatures, error
  variants, and module visibility must be extracted and specified precisely — not described
  in prose that leaves room for interpretation. A behavioral description that permits two
  valid API shapes is an underspecified requirement.

- **When two approaches exist, choose the one that produces more determinism.** Do not
  optimize for speed or simplicity at the cost of precision. A slower step that pins an
  interface exactly is always preferable to a faster step that leaves it ambiguous.

- **Ambiguity in requirements is a bug.** If the requirements document permits an
  implementation that compiles but fails tests, the requirements are wrong. Iteration
  exists to eliminate that ambiguity, not to tolerate it.

- **The anti-corruption layer is interface contracts.** Code that will be called by
  other code — across crate boundaries, across module boundaries, across trait impls —
  must have its exact public interface specified as a hard constraint before code
  generation begins. Behavioral requirements describe what the code does; interface
  contracts describe what the code looks like from outside. Both are required.

---

## Pipeline Overview

```
STARTUP (single command: python pipeline.py --project <name>)
  ├── Extract test command from README (AI, once)
  ├── TASK 0a: Build project_profile.md from README + test paths (AI, once, cached)
  └── TASK 0b: Build dependency graph from manifests (static parse, once)
               → topological module order (leaves first)

AUTO PRE-STRIP (runs automatically when .coverage_augmented is absent or coverage < 100%)
  └── TASK 0c: Coverage augmentation
        Per module: measure coverage (tarpaulin) → generate tests for
        uncovered lines → compile-fix loop → re-measure → repeat until 100%
        Writes .coverage_augmented sentinel (with overall coverage %).
        → strip_to_tests.py runs automatically after augmentation completes.
        (Force re-run with --augment-tests flag regardless of sentinel state.)

GLOBAL (once per run)
  ├── TASK 1: Requirements extraction
  │     All test files (chunked if large) + README + genesis.md
  │     → functional_requirements.md (typed API contracts)
  │     Skipped if file already exists (resume support)
  │
  └── TASK 1.5: Static use-statement audit (no LLM)
        Parse all `use` paths from test files, diff against functional_requirements.md.
        Logs gaps as warnings — does not block the pipeline.

MODULE LOOP (per module in topological order)
  │
  ├── TASK 2 inner loop: Code generation + compile-fix cycle
  │     ├── Generate source files for this module only
  │     │     Input: functional_requirements.md + locked dep source as context
  │     ├── BUILD CHECK: compile only (no tests yet)
  │     │     ├── OK  → exit inner loop
  │     │     └── FAIL → targeted fix (only broken files) → re-check (max N times)
  │     └── Escalate to TASK 4 if inner loop exhausted
  │
  ├── TASK 3: Run module-scoped tests (e.g. cargo test -p <module>)
  │     ├── All pass → LOCK module → inject source as context for dependents
  │     └── Failures → targeted fix loop (only broken files) → re-run
  │               └── Still failing → TASK 4
  │
  ├── TASK 4: Failure analysis + genesis update
  │     Compare pass count vs previous iteration (regression detection)
  │     Classify: build error → structural analysis / test failure → gap analysis
  │     Update genesis.md with general lessons
  │     Append to failure_log.md
  │     Delete this module's generated files → retry from TASK 2
  │
  └── Genesis audit (after TASK 4)
        Triggered on regression OR every 5 total iterations.
        LLM reviews genesis.md for contradictions and redundant rules.
        Resolves issues in-place — no new rules added.

FINAL
  └── Full workspace test suite — validates integrated result

OUTPUT: genesis.md (accumulated methodology)
```

---

## Files and Their Role

| File | Persists | Purpose |
|------|----------|---------|
| `genesis.md` | Yes | Primary research artifact. Accumulates reasoning methodology. |
| `project_profile.md` | Yes | Language, build system, workspace structure. Built once (TASK 0a). |
| `failure_log.md` | Yes | Running record of what each iteration tried and what happened. Feeds TASK 4. |
| `functional_requirements.md` | Per run (once globally) | Requirements produced by TASK 1. Skipped on resume if exists. |
| `.generated_files_<module>.txt` | No (wiped per module on retry) | Manifest of source files written by AI for a module. |
| `tests_output_{module}_{N}.txt` | Yes (audit) | Genesis snapshot + test output. One per module iteration. |
| `run.log` | Yes (append) | Full audit trail of all runs and restarts. |
| `.task1_partials/` | Temporary | Per-chunk partial requirements. Cleared after synthesis. |
| `.locked_modules/locked.txt` | Yes | Set of locked module names. Survives restarts. |
| `.locked_modules/<name>/` | Yes | Backup of locked source files. Injected as context for dependents. |
| `.coverage_augmented` | Yes (sentinel) | Marks TASK 0c complete. Prevents re-running augmentation. |

---

## Task Details

### TASK 0c — Coverage Augmentation (pre-strip, `--augment-tests` flag)

**When:** run once against the full project **before** `strip_to_tests.py`.
**Input:** real source + existing tests, `cargo-tarpaulin` for coverage measurement.
**Output:** new `#[cfg(test)]` blocks written into source files until 100% coverage per module. Sentinel `.coverage_augmented` written on completion.

**Loop per module:**
1. `cargo check` to verify it compiles — skip module if it doesn't.
2. Run `cargo-tarpaulin` to measure coverage.
3. If < 100%: ask LLM to generate tests for the uncovered lines.
4. Compile-fix loop (`generate_fix_loop`) until tests compile cleanly.
5. Re-measure coverage. Repeat from step 3 until 100% or retries exhausted.

**Why:** modules like `argh_shared` ship with zero unit tests. Without augmentation,
`cargo test` exits 0 on an empty binary — a false "passing" signal. After stripping,
the pipeline would have no test signal for that module. Augmentation ensures every
module has a complete, meaningful test suite before the source is removed.

---

### TASK 0a — Project Profile (once per run, cached)

**Input:** README.md + test file paths (not contents)
**Output:** `project_profile.md`

Asks the model to identify the project's language, build system, workspace layout,
and minimum required files per module — derived from README and test path conventions
alone, not from file contents. Cached: skipped if `project_profile.md` already exists.

**Key constraint:** Only test paths are given, not content. This forces the model
to reason from naming conventions (e.g., `streamer/tests/`, `accounts_db/src/tests.rs`)
rather than reading implementation details.

---

### TASK 0b — Dependency Graph (once per run, no LLM in static mode)

**Input:** workspace manifests (`Cargo.toml` etc.)
**Output:** topological module order (leaves first), module types, scoped test/build commands.

Two backends selected by `DEPENDENCY_GRAPH` in `.env`:
- `static` (default): deterministic Python parsing of `Cargo.toml` — fast, Rust-only.
- `llm`: feeds all manifests to the model — language-agnostic.

Produces the processing order for the module loop and owns the logic for scoping
workspace commands to a single module (e.g. `cargo test` → `cargo test -p argh`).

---

### TASK 1 — Requirements Extraction

**Input:** all test file contents + README + genesis.md + project_profile.md
**Output:** `functional_requirements.md`

The model applies the methodology in genesis.md to extract every behavior, interface,
data structure, error condition, and invariant that a correct implementation must satisfy.

**Chunking strategy (when test files exceed context budget):**
1. Split test files into chunks that fit within the model's context window
   - Large individual files are split at test function / test block boundaries,
     not at arbitrary character positions. Each sub-chunk is labeled `file (part N/M)`.
   - A brief project context header is prepended to every chunk prompt so the model
     knows what it is looking at even without the full file.
2. Each chunk is processed independently (with retries). Partial results are saved
   to `.task1_partials/` so a restart resumes without reprocessing completed chunks.
3. A final synthesis call combines all partial results into `functional_requirements.md`,
   deduplicating and reconciling contradictions.

**What good requirements look like:**
- Specific interface signatures, not vague descriptions
- Edge cases and error conditions, not just happy paths
- Invariants (what must always be true), not just inputs/outputs
- Concrete types and return values where tests assert them

**What to avoid in requirements:**
- Over-specification (dictating implementation: "use a HashMap")
- Under-specification ("handle errors appropriately")
- Test-specific language ("when test_foo runs...")

---

### TASK 2 — Source Code Generation

**Input:** functional_requirements.md + project_profile.md + workspace structure
**Output:** source files written to disk, tracked in `.generated_files.txt`

The model generates all source files needed to satisfy the requirements. The workspace
structure (which crates exist, which declare a lib target, which declare a bin target)
is read statically from existing `Cargo.toml` / `package.json` / build files and
passed explicitly — the model does not guess it.

**Key rules enforced by the pipeline:**
- Only files with known source extensions can be created or overwritten
- Test files are never overwritten (protected by path matching against scanned test set)
- Config files (`Cargo.toml`, `package.json`, etc.) are never overwritten

**Common failure modes in TASK 2:**
- Missing `src/lib.rs` or `src/main.rs` stubs in crates that declare them
- Missing `pub mod foo;` declarations in lib.rs for sibling modules
- Wrong directory structure (flat instead of per-crate)
- Hallucinated external dependencies not in the project's manifest

---

### BUILD CHECK — Compile-only Validation (inner loop)

**Input:** current state of source files on disk
**Output:** compiler success or compiler error message

Runs the project's compile-only command (e.g., `cargo check`) before running
the full test suite. If it fails, the compiler error is fed back to TASK 2 and
the model retries code generation — without going through TASK 1 again.

This inner loop can retry up to 5 times. Most build errors are structural
(missing file, wrong module declaration) and are faster to fix than to re-derive
requirements for.

**Why this matters:**
Build errors are categorically different from test failures. A build error means
the code doesn't compile — the requirements and genesis.md are not at fault. Routing
build errors through TASK 4 (genesis update) wastes iterations and pollutes genesis.md
with structural fix instructions instead of methodology improvements.

---

### TASK 3 — Run Full Test Suite

**Input:** test command (discovered once at startup)
**Output:** `tests_output_{N}.txt` (genesis snapshot + full test output)

Pure execution — no AI involved. The pipeline runs the test command in the
project directory, captures stdout + stderr, and saves everything to disk.

Each `tests_output_{N}.txt` begins with the full genesis.md that produced it,
making it a self-contained snapshot: you can reconstruct exactly what the methodology
was when any result was produced.

If all tests pass: the process is complete. genesis.md is the output artifact.

---

### TASK 4 — Failure Analysis and Genesis Update

**Input:** current test output + previous test output + genesis.md + failure_log.md
**Output:** updated `genesis.md` + new entry in `failure_log.md`

The model analyzes what went wrong, compares this iteration to the previous one,
and updates genesis.md with general lessons.

**The comparison logic:**
- Count passing tests in iteration N vs N-1
- If N > N-1: improvement. Reinforce the approach, refine the rule.
- If N == N-1: no progress. The fix was cosmetic. Try a substantively different approach.
- If N < N-1: regression. The last genesis update made things worse. Roll back that
  specific change and try a different direction.

**What goes into genesis.md (general lessons only):**
- "When tests assert on module visibility, functional requirements must explicitly
  enumerate public interfaces" — general rule
- Not: "iteration 7 failed because streamer/src/lib.rs was missing" — specific symptom

**What goes into failure_log.md (the memory of what was tried):**
- Iteration number
- Pass count (before and after)
- Root cause classification (build error / missing requirement / wrong type / logic error)
- What genesis.md change was made
- Whether it helped

The failure log prevents the model from repeating failed approaches in future iterations.
Without it, the model re-derives the same (wrong) fix from the same test output.

---

### Cleanup — Between Module Iterations

After a module fails (TASK 4), the pipeline deletes that module's generated files
(tracked in `.generated_files_<module>.txt`) and recreates a minimal workspace stub
(`lib.rs` / `main.rs`) so Cargo can still parse the workspace. Locked modules are
never touched. `functional_requirements.md` persists for the entire run — it is only
rebuilt if it is missing at startup.

---

## Known Failure Modes and Mitigations

### Hallucination in chunked TASK 1

**Problem:** When test files are split across chunks, the model processes each chunk
independently. It cannot see the full file at once, so it may invent behavior that
doesn't exist or miss behavior that depends on context across the split boundary.

**Mitigations:**
- Split at semantic boundaries (test function / test block), not character positions
- Prepend a project context header to every chunk prompt
- The synthesis step is explicitly asked to reconcile contradictions across chunks
- Chunk partial results are labeled with file name and part number so the synthesis
  step knows they belong together

### Symptom-driven genesis updates

**Problem:** The model sees a specific test failure and adds a specific rule to genesis.md
("always generate src/lib.rs") rather than a general lesson ("when a crate declares a
lib target in its manifest, a library entry point file must exist").

**Mitigation:** The TASK 4 prompt explicitly asks for the general class of error, not the
specific symptom. The failure log shows the model what specific fixes were already tried
and failed, pushing it toward more general reasoning.

### Build errors consuming outer loop iterations

**Problem:** If code generation produces non-compiling code, the full outer loop runs:
TASK 1 (expensive, many chunks) → TASK 2 → TASK 3 (build fails immediately) → TASK 4
(updates genesis.md with structural fix). This wastes hours on a fix that could take seconds.

**Mitigation:** The inner loop (BUILD CHECK) retries TASK 2 with compiler feedback up to
5 times before escalating. Only unresolvable build failures escalate to the outer loop.

### Genesis pollution from wrong lessons

**Problem:** If a genesis update teaches the wrong lesson, every future iteration inherits
that wrong rule. Over time, genesis.md can accumulate contradictory or misleading rules.

**Mitigations:**
- The failure log tracks whether each change helped or regressed results
- TASK 4 explicitly compares pass count and is instructed to roll back approaches
  that caused regressions
- (Future) periodic genesis audit: review genesis.md for contradictions before iteration

### Context window truncation

**Problem:** Individual test files in large projects (like agave) can exceed the model's
context window. When truncated, the model only sees a fragment but responds as if it saw
the whole thing, producing requirements that reflect only the beginning of the file.

**Mitigation:** The chunking logic splits large files at test boundaries into labeled
sub-chunks, ensuring every part of every file is processed completely. The context budget
accounts for prompt overhead (README + instructions + system context) so content chunks
always fit.

---

## Design Constraints

**genesis.md must be self-contained and project-independent.**
It cannot mention `agave`, `Solana`, specific test names, specific file names, iteration
numbers, or any detail tied to this experiment. Every rule must be abstract enough to
apply to a new project in a different language.

**Test files and build files are never modified.**
The pipeline only creates and deletes source files in the extension whitelist.
Config files (`Cargo.toml`, `package.json`, etc.) and test files are protected unconditionally.

**The model derives structure, it does not receive it as a hint.**
With one exception: the workspace structure is read from existing build files and passed
to TASK 2 explicitly, because test files do not encode "this crate needs src/lib.rs" —
that information is only in `Cargo.toml`. Everything else (language, framework, module
organization, types) must be derived from tests and README.

**The process must be resumable.**
A crash or manual stop at any point should be recoverable. The pipeline detects its
resume point from the state of files on disk. Partial chunk results are saved to disk.
The manifest tracks what was written. The run log is append-only.

---

## Hardware Context

**Current machine:** HP Victus 15L Gaming Desktop
- GPU: RTX 4060 — 8 GB VRAM
- RAM: 15 GB
- Active model: codestral (12 GB) — split 5.2 GB GPU / 6.8 GB CPU

**Implication:** codestral runs with ~44% of layers on GPU. Inference is 3-5x slower
than full-GPU. Each chunk takes 2-5 minutes. A full TASK 1 pass over the agave project
takes several hours.

**Planned upgrade:** RTX 4060 Ti 16GB (drop-in, same TDP). Would run codestral fully
on GPU, reducing TASK 1 from hours to ~20-30 minutes.

**Ollama configuration required:**
```
OLLAMA_MODELS=/usr/share/ollama/.ollama/models   # where models are stored
OLLAMA_CONTEXT_LENGTH=8192                        # safe for 15 GB RAM with codestral
OLLAMA_NUM_CTX=8192                               # passed per-request via extra_body
```

---

## Iteration History Summary

| Iteration | Model | Outcome | Root Cause |
|-----------|-------|---------|------------|
| 1 | qwen2.5-coder:7b | Build fail | Test command extracted with wrong format (`./cargo`) |
| 2 | qwen2.5-coder:7b | Build fail | `cargo` not on PATH, then found; manifest errors |
| 3 | qwen2.5-coder:7b | Build fail | Generated Python/Go files instead of Rust |
| 4 | qwen2.5-coder:7b | Build fail | TASK 4 failed all 3 retries; genesis unchanged |
| 5 | qwen2.5-coder:7b | Build fail | TASK 4 failed all 3 retries; genesis unchanged |
| 6 | qwen2.5-coder:7b | Build fail | Generated Go/proto files; TASK 4 failed again |
| 7 | qwen2.5-coder:7b | Build fail | Generated Rust stubs but missing `src/lib.rs` |
| 8 | codestral | In progress | Context window and chunking being fixed |

**Pattern:** qwen2.5-coder:7b consistently failed TASK 4 (couldn't produce the
`===GENESIS_START===` delimiter format). Genesis never updated. The loop learned nothing.

**Codestral upgrade rationale:** Better instruction following for structured output formats.
Better Rust code generation. Trades speed (too large for full GPU) for quality.

---

## What Good Looks Like (Definition of Done per iteration)

A productive iteration:
1. TASK 1 produces requirements that are specific, complete, and non-contradictory
2. TASK 2 generates files that compile on the first or second inner-loop attempt
3. TASK 3 shows measurable progress: more tests pass than the previous iteration
4. TASK 4 produces a genesis update that generalizes the lesson (not patches the symptom)
5. failure_log.md has a new entry with an honest assessment of what changed and why

The process is complete when all tests pass. At that point:
- `genesis.md` is the output — publish, study, apply to new projects
- The reconstructed source code is disposable
- `tests_output_{N}.txt` files form a complete audit trail of the methodology's evolution

---

## Step Payload Contracts and Anti-Corruption Validation

Each task must produce output that meets the contract below before the next task starts.
A `_validate_step_output(step, context)` function enforces these checks in `pipeline.py`;
violations raise `StepOutputInvalid(reason)` and the caller retries or escalates.
Validators are deterministic (no LLM) and run in milliseconds.

---

### TASK 0a — project_profile.md

**Output contract:**
- Length ≥ 300 chars
- At least one module entry whose directory path exists on disk
- Build system identifier matches a file on disk (e.g. "Cargo" requires `Cargo.toml`)

**Anti-corruption check before TASK 1:**
- Every module path listed in the profile exists as a directory on disk
- No module typed as "bin" when `Cargo.toml` declares `[lib]`, or vice versa
- If any check fails: re-run TASK 0a, do not proceed

**Cache invalidation:** profile is per-project (stored in `project_dir/`, not pipeline root).
Regenerated when the SHA-256 of (README + test file paths) changes from the stored hash.

---

### TASK 0b — dependency order

**Output contract:**
- At least one module in the list
- Every module listed appears in `project_profile.md`
- No module appears twice
- Order is valid: no module precedes a module it depends on per the manifests

**Anti-corruption check before module loop:**
- Topological order verified programmatically
- If invalid: re-run TASK 0b, do not start module loop

---

### TASK 0c — augmented source files

**Output contract:**
- Coverage of target module strictly higher than baseline after augmentation
- `cargo test -p {module} --no-run` exits 0 (all appended blocks compile)
- At least one new `#[test]` function added
- All appended blocks syntactically complete (matching braces)

**Anti-corruption check before stripping:**
- Final coverage measurement ≥ configured threshold (default 80%)
- Compile check passes; if not: log warning but do NOT advance if compile check fails
- Pre-clean regex targets only pipeline-generated blocks (`mod gen_\w+`), never `mod tests`

---

### TASK 1 — functional_requirements_{module}.md

This is the most critical boundary. Failures here propagate silently through every downstream task.

**Output contract:**
- Length ≥ 500 chars
- At least 3 distinct type or function names that appear literally in the module's test files
- At least one field-level or parameter-level type specification (not just "has a timeout field")
- Document is not generic meta-commentary (must not contain phrases like "functional requirements are" or "this document describes")
- Does not describe the pipeline itself

**Anti-corruption check before TASK 2:**
1. Extract all identifiers (CamelCase + snake_case) from the requirements doc
2. Intersect with identifiers from the module's test files
3. If intersection < 3 elements: requirements too generic → retry TASK 1 (max 2 retries)
4. If length < 500 chars: requirements too sparse → retry TASK 1
5. If meta-commentary detected → retry TASK 1
6. After 2 failed retries: escalate with `failure_type="requirements_failure"` to TASK 4

---

### TASK 2 — generated source files

**Output contract:**
- All file paths from the original structure for this module are present on disk
- Each generated file is non-empty (≥ 50 chars) and contains actual source code
- No file consists only of placeholders (`unimplemented!()` on every function, or only comments)
- Public API exported matches what the test files import (every `use {module}::{Name}` resolves)

**Anti-corruption check before TASK 3:**
- All expected paths exist; if any missing: send back to TASK 2 with specific missing-file error
- `cargo check -p {module}` passes (1 fix retry allowed before escalating)
- At least one type definition per type named in `functional_requirements_{module}.md`

---

### TASK 3 — test results

**Output contract:**
- `tests_run > 0`: zero tests is not a pass, it is `failure_type="no_tests"`
- `passed + failed == tests_run`
- Failure messages include test name, failure type, and actual vs. expected values

**Anti-corruption check before TASK 4:**
- If `tests_run == 0`: diagnose WHY before calling TASK 4
  - Does the module have any test files? → if no test files, mark as `no_tests`, generate stub, move on — do NOT loop
  - Does the test binary compile? → compile failure, not a test failure
  - Is the test filter excluding everything? → misconfiguration
- If `tests_run > 0` and all pass: lock module, do not call TASK 4

**The no_tests loop rule:** A module with no test files must never enter the TASK 1→2→3→4 loop.
Mark it as `no_tests`, generate a minimal stub implementation, and advance. Discovered
in iteration 56+ of `argh_shared` — the pipeline cycled indefinitely because there was no
boundary check for "module has zero test files."

---

### TASK 4 — updated genesis.md

**Output contract:**
- `genesis.md` length after update > length before
- New content references at least one concrete identifier from the failing test output
- New content does not repeat verbatim any existing section (deduplication check)
- New content does not contradict an existing lesson

**Anti-corruption check before next TASK 1:**
- If `genesis.md` is identical to before: log `failure_type="genesis_stall"` prominently
- A genesis stall means TASK 4 has no new insight — the failure is structural, not a requirements gap
- Do not run another TASK 1 immediately on a genesis stall; surface it and halt

---

### Context Window Contracts

**Current hardware:** RTX 3090 24 GB — qwen3:32b (Q4_K_M, ~20 GB) fits fully on GPU.
**Context:** `OLLAMA_NUM_CTX=32768`. Safe content budget per call: `(32768 − ~6k overhead) × 4 chars ≈ 107k chars`.

**Rule:** No artificial content limit smaller than what the model can process.
If a call exceeds the context window, Ollama returns an observable error — diagnosable and fixable.
Silent truncation of type definitions, error messages, and test files produces wrong code that looks
plausible and is much harder to debug.

| Constant | Was | Now | Reason |
|---|---|---|---|
| `SINGLE_CALL_CHAR_LIMIT` | 20k | 80k | conservative 75% of 107k budget |
| `FILE_CHAR_LIMIT` | 20k | 90k | per-file limit before semantic split |
| `INTERMEDIATE_CHAR_LIMIT` | 16k | 60k | stage-1 group merge content budget |
| `TASK4_DOC_LIMIT` | 20k | 200k (unused) | genesis/FR now passed in full; constant kept for compatibility |
| fix loop per-file cap | 2k | full file | broken code is often past line 60 |
| fix loop total cap | 12k | full file | every file with an error must be visible |
| compile error cap | 800 | full output | all errors shown, not just the first |
| pre-flight file cap | 6k | full file | appended test block is at the END |
| existing tests cap | 2k | full block | truncated examples end mid-function |
| proc-macro test cap | 24k | removed | all derive-attribute tests in full |
| manifest per-file cap | 4k | removed | manifests are config, not source code |
| failure log cap | 8k | removed | compact log; 100 iterations ≈ 15k chars |

---

## Future Improvements Backlog

- [x] **TASK 1.5: Static use-statement audit** — Implemented. Pure Python, no LLM. Parses
      `use` paths from test files after TASK 1, logs any that are missing from
      `functional_requirements.md`. Warnings only — does not block the pipeline.
- [x] **Genesis audit step** — Implemented. LLM reviews genesis.md for contradictions and
      redundant rules after every TASK 4. Triggers immediately on regression or every 5
      total iterations. Uses `_TASK4_MODEL`.
- [ ] **TASK 1b: Interface contract extraction (dedicated pass)** — Skipped for now.
      `LLMTask1Synthesizer` already performs contract-first extraction; a separate pass
      would largely duplicate it.
- [ ] **TASK 0c: non-Rust support** — Coverage augmentation currently requires
      `cargo-tarpaulin`. Needs a coverage backend for Python (coverage.py), JS (c8/istanbul),
      Go (go test -cover), etc. before it can run on non-Rust projects.
