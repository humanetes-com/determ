# Functional Requirements — determ pipeline

This document records the design decisions for `pipeline.py`. Every significant architectural
or behavioral choice lives here so that any future session (human or AI) can understand
what was decided and why.

---

## FR-001 — File-by-file test extraction (TASK 1)

**Decision:** Each test file is processed as its own LLM extraction call. Never bundle
multiple files into one chunk. Never split a file unless it individually exceeds the
context window.

**Why:** Chunking by character budget (the previous approach) produced 17 chunks for the
argh project, meaning the model saw 1/17th of the test suite per call. This caused:
- Fragmented contract extraction: cross-file types and traits were never visible together
- Information loss through multi-level synthesis (17 chunks → 2 group merges → final)
- Timeout errors on large merge calls (merging 10 partials at once exceeded Ollama's timeout)

One file = one call gives the model a complete, coherent, named unit. The synthesis step
receives one partial per file (18 for argh), which is smaller and more focused than
receiving summaries of summaries.

**Implementation:**
- `chunk_test_files_by_file(test_files, FILE_CHAR_LIMIT)` in `pipeline.py`
- `FILE_CHAR_LIMIT = 90_000` — only files exceeding this are split at semantic boundaries
- Replaces `chunk_test_files(test_files, CHUNK_CHAR_LIMIT)` in `StaticTask1Synthesizer._chunked`

**Hardware note:** With `OLLAMA_NUM_CTX=32768` (RTX 3090), the largest argh test file is
52k chars (~13k tokens), well within context. File-by-file is both feasible and correct.

---

## FR-002 — Context window constants must match hardware

**Decision:** `SINGLE_CALL_CHAR_LIMIT` and `FILE_CHAR_LIMIT` must reflect the actual
`OLLAMA_NUM_CTX` set in `.env`. When hardware changes, update both `.env` and these constants.

**Rationale:**
- RTX 4060 8GB: `OLLAMA_NUM_CTX=8192` → `CHUNK_CHAR_LIMIT=20_000` (old, now removed)
- RTX 3090 24GB: `OLLAMA_NUM_CTX=32768` → `FILE_CHAR_LIMIT=90_000`, `SINGLE_CALL_CHAR_LIMIT=80_000`

At 4 chars/token with 32k context, minus ~6k tokens for prompt and response overhead:
content budget ≈ 26k tokens ≈ 104k chars per call. 90k is the conservative safe limit.

---

## FR-003 — Bottom-up module processing order

**Decision:** Modules are processed leaf-first (topological order from `Cargo.toml`).
Each locked module's source is injected as context for dependent modules.

**Why:** Prevents type mismatches between modules generated without knowledge of each
other. A module that depends on `argh_shared` must use the exact types `argh_shared`
exports — not invent its own.

---

## FR-004 — Autopilot never writes project source files

**Decision:** The autopilot (Claude Code) only modifies `pipeline.py`, `.env`, and
shell scripts. It never writes `.rs`, `.py`, or other source files inside project
subdirectories.

**Why:** Role separation is the core invariant of the experiment. If the autopilot
writes source code, it is doing the LLM's job and invalidating the research.

---

## FR-005 — No ANSI color codes in any output

**Decision:** All scripts and pipeline output use plain text. No `\033[`, `\e[`, tput,
or color variables.

**Why:** The user runs inside GNU screen, which renders escape codes as raw garbage.

---

## FR-007 — Ollama client must have no timeout

**Decision:** The `OpenAI` client used for Ollama must be created with `timeout=None`.

**Why:** qwen3:32b on large prompts (synthesis, multi-partial merges) takes longer than
the default OpenAI SDK timeout (600s). Since Ollama is local, there is no network risk
and no reason to cap the wait. A timeout here causes silent retry loops and wasted time.

**Implementation:** `OpenAI(base_url=..., api_key="ollama", timeout=None)` in `model_interact.py`.

---

## FR-008 — Synthesis group size must skip Stage 1 for small projects

**Decision:** `SYNTHESIS_GROUP_SIZE = 20`. When total partials ≤ group size, Stage 1
(intermediate group merges) is skipped and all partials go directly to Stage 2 (final
synthesis).

**Why:** With 18 test files (argh), Stage 1 merges 10 partials at once — a large prompt
that timed out repeatedly. With group size 20, argh's 18 partials skip Stage 1 entirely:
one final synthesis call receives all 18 directly, which is ~18 × 1500 chars ≈ 27k chars
— well within 32k context. Fewer LLM calls, no intermediate information loss.

---

## FR-006 — Single start command, fully self-managing

**Decision:** `python pipeline.py --project <name>` is the only command needed. The
pipeline detects its own state (coverage sentinel, locked modules, cached partials) and
resumes or starts as appropriate.

**Use `--clean` only** when state is corrupt: missing manifests, broken workspace,
or after a design change that invalidates cached state (e.g. FR-001 implementation).

---

## FR-009 — Pre-flight startup script (`start.sh`)

**Decision:** All startup checks are codified in `start.sh`, not embedded in `pipeline.py`.
The script is the recommended entry point instead of invoking `pipeline.py` directly.

**Checks performed (in order):**
1. `.env` exists
2. `.venv` exists; all Python dependencies importable (`anthropic`, `openai`, `dotenv`)
3. Project directory exists
4. VRAM (via `nvidia-smi`) → recommended `OLLAMA_NUM_CTX` → compare with `.env`; suggest correction if mismatched
5. Ollama server responding; offer to start it if not
6. Model available locally in Ollama (or API key set for Claude models)

**Behavior per failed check:** print the problem + a concrete suggestion → prompt `[y/N]`.
- `y`: applies the suggestion and continues.
- `n` or non-interactive: stops and prints `bash start.sh --project X --force`.

**`--force` flag:** skips all prompts, applies every suggestion automatically. Useful for
scripted/unattended runs where the operator has already verified the config.

**Usage:**
```bash
bash start.sh --project argh-master
bash start.sh --project argh-master --force
bash start.sh --project argh-master --clean
```

**Why:** Silent misconfiguration (wrong `OLLAMA_NUM_CTX`, Ollama not running, missing deps)
previously caused hours of wasted compute or silent failures. Codifying the checks ensures
every run starts from a validated state without requiring the operator to memorise the
pre-flight procedure.

---

## FR-010 — Topological file generation order (same principle as FR-003)

**Decision:** In `StaticFileListTask2Generator`, files within a module are generated in
topological dependency order. The crate entry point (detected from `Cargo.toml`, defaulting to
`src/lib.rs` / `src/main.rs`) is generated first. After each file is generated, its `mod foo;`
declarations are parsed and used to re-sort remaining files via `graphlib.TopologicalSorter`.

**Why:** The generator makes one LLM call per file, passing already-generated files as context.
Alphabetical ordering caused `bash.rs` and `fish.rs` to be generated before `lib.rs` — the
model had no API context and produced empty output. This is the same problem FR-003 solves at
the module level (leaf-first topological order); FR-010 applies the same principle at the
intra-module file level.

The entry point has 0 intra-crate dependencies. Every file it declares via `mod foo;` depends
on it. The file with fewest dependencies generates first — its name is irrelevant.

**Implementation:**
- `_crate_entry_point(cargo_toml, module_dir_rel, file_path_set)` — reads `[lib]`/`[[bin]]`
  path from Cargo.toml; falls back to `src/lib.rs` then `src/main.rs`.
- `_topo_sort_files(file_paths, known_content)` — parses `mod foo;` from `known_content`,
  builds a `graphlib.TopologicalSorter` dependency graph, returns sorted order.
- Generation loop: entry point first, then after each generated file, re-sort remaining
  files using the new mod declarations. Cycle/error → keep current order.

---

## FR-011 — Systemic approach to pipeline challenges

**Decision:** When the autopilot identifies a recurring failure or structural weakness in the
pipeline, the response must be:
1. Identify the root cause as a systemic design gap (not a one-off quirk).
2. Document the decision in `FUNCTIONAL_REQUIREMENTS.md` as a new FR entry.
3. Implement the fix in `pipeline.py`.

Ad-hoc tactical patches without a corresponding FR entry are not allowed. If a problem
is worth fixing, it is worth recording why and how.

**Why:** Undocumented fixes create invisible invariants. Future sessions (human or AI) cannot
understand why code behaves a certain way, leading to the same problems being rediscovered or
the fix being accidentally reverted.

---

## FR-012 — Per-file retry in StaticFileListTask2Generator

**Decision:** Each individual file generation call in `StaticFileListTask2Generator` retries
once (2 attempts total) if the response is empty or unparseable.

**Why:** qwen3:32b in thinking mode occasionally generates a long `<think>...</think>` block
and then produces empty output — no code, no file markers, no code fence. Without retry, the
file is simply skipped, causing a build failure (`mod bash;` declared in lib.rs but bash.rs
missing). A single retry resolves non-deterministic empty responses. Systematic empty responses
(model consistently produces nothing for a specific file across all attempts) indicate a
requirements problem — see FR-013.

**Implementation:** The per-file generation loop in `StaticFileListTask2Generator.generate()`
wraps the `ask()` call in a `for _attempt in range(2):` loop, breaking on success and logging
a retry message on the first empty response.

---

## FR-013 — Requirements must capture behavioral contracts, not just type signatures

**Decision:** When a TASK 1 extraction produces only interface signatures (trait definitions,
struct shapes) without describing what the implementation actually produces, TASK 2 will
systematically fail to generate those files — the model "over-thinks" and outputs nothing
because it cannot determine what correct behavior looks like.

The fix is in the pipeline's TASK 1 prompt: it must explicitly ask for behavioral contracts
(example inputs/outputs, format descriptions, invariants) in addition to type signatures.
A requirement like `impl Generator for Bash { fn generate(...) -> String }` is insufficient.
A requirement like `impl Generator for Bash — generate() returns a Bash completion script
string for the given command, listing all flags with --flag (description)` is actionable.

**Why:** Observed during argh_complete generation: bash.rs and fish.rs returned empty on every
attempt across multiple retries and restarts. Both implement the same `Generator` trait as
nushell.rs and zsh.rs (which generated successfully). The difference was not the model, not
the ordering, not the context — the requirements gave the model no signal about what the
output string should contain, triggering a systematic think-then-output-nothing failure.

**Implementation:** In `task1()` / `LLMTask1Synthesizer`, the extraction prompt must include:
- For each public function/method: what does it return? What format? What invariants?
- For trait implementations: what does each impl actually DO, not just the signature.
- If test output assertions exist: include them verbatim as behavioral proof.

---

## FR-014 — TASK 0c must produce behavioral specifications, not just coverage hits

**Decision:** The TASK 0c prompt must explicitly require that every generated test contains
at least one assertion (`assert!`, `assert_eq!`, `assert_ne!`, or an `unwrap()` that panics
on failure). Tests that call a function without asserting its output are rejected.

**Why:** TASK 0c runs before stripping. Its augmented tests become the test suite that TASK 1
reads to extract functional requirements. An assertion-free test ("call the function, don't
assert anything") passes coverage metrics but provides zero specification signal to TASK 1.
The consequence: TASK 1 extracts thin requirements → TASK 2 generates thin code → the loop
runs for many iterations improving nothing. The root cause is a metric (coverage%) that is
a proxy for specification quality, not the real goal. Behavioral assertions are the real goal.

**Implementation:** The TASK 0c prompt must state: "Every test must contain at least one
assert!, assert_eq!, assert_ne!, or an unwrap() call that verifies the function's output or
side effect. Tests that only call a function without asserting are invalid and will be
discarded." The prompt must also instruct the model to prefer testing edge cases and error
conditions over happy paths.

---

## FR-015 — TASK 0c must show the full function body, not just the uncovered line annotation

**Decision:** When TASK 0c generates tests for uncovered lines, the model must see the
complete body of the function containing those lines — not just a narrow ±4 line annotation
around the uncovered position.

**Why:** The focused annotation (`_focused_annotation`) shows only the lines around where
coverage is missing. To write a test with a behavioral assertion, the model must understand
what the function *does*: its return type, the values it produces, the error conditions it
raises. All of this is in the function body, which is typically 10–50 lines. The focused
annotation shows at most 8 lines. The model cannot write `assert_eq!(result, expected_value)`
without knowing what `expected_value` is — that knowledge comes from reading the full logic.

**Implementation:** Add `_extract_uncovered_functions(content, uncovered_lines)` which:
1. Parses the source file to find all `fn` definitions
2. For each `fn` that contains ≥1 uncovered line, extracts its complete body (from the
   opening `fn` line to the matching closing brace)
3. Annotates uncovered lines with `// !! UNCOVERED` in the output
4. Returns all matching function bodies separated by a divider
Falls back to `_focused_annotation` if no function bodies can be identified.

---

## FR-016 — Synthesize project_context.md from test content as the portable big-picture document

**Decision:** Add a `task0_project_context()` step that produces `project_context.md` — a
2–3 paragraph behavioral summary of what the project does and what each module's role is.
This runs once after TASK 0a, is cached with the same hash as the project profile, and is
injected into every task that needs project-level context (especially TASK 0c, which
currently receives none).

**Why:** Every task in the pipeline operates on a slice of context relevant to its job.
No task currently receives a stable, synthesized understanding of what the project does as
a whole. README.md serves this role when present, but may be absent, out of date, or too
general. `project_context.md` is synthesized from test file content (which is always present)
and the project profile, making it project-agnostic and README-independent. This is the
"big picture" that prevents tasks from treating the project as an anonymous set of files.

**Implementation:** `task0_project_context(context_path, test_files, readme_text, profile)`
calls the LLM with a prompt that includes: README (if present), the project profile, and the
first 800 chars of each test file (up to 25 files). The output is a plain-text synthesis:
what the project does, what its public API looks like at a high level, what each module's
role is. Cached: regenerated only when the same inputs hash changes. Injected into TASK 0c
(as the sole source of project context there) and passed alongside README in TASK 1.

---

## FR-017 — README-absent is the baseline; README is an enhancement

**Decision:** Every pipeline task must function correctly with no README.md. When README is
absent or empty, `project_context.md` (FR-016) is the sole source of project-level context.
No task may silently omit its context section when README is empty — it must fall back to
`project_context.md`.

**Why:** The pipeline is designed to be generic: the only guaranteed input is a folder
containing tests (and optionally build manifests). Projects in the wild often have minimal
or no README. Designing around README presence makes the pipeline brittle. The test files
themselves are the authoritative specification; `project_context.md` is the synthesized
distillation of that specification into human-readable form. Any task that needs project
context should accept either README or project_context, not require README specifically.

**Implementation:** In all task prompts that include a `README.md:` section, replace the
raw `readme_text` variable with `effective_context = project_context or readme_text` — the
synthesized context takes priority, with the raw README as fallback. TASK 0c receives
project_context as a new parameter and includes it as a "Project context" section at the
top of every per-file prompt.

---

## FR-018 — TASK 0c must reach the configured coverage target; existing tests are preserved unconditionally

**Formal definition of "sufficient coverage".** "High enough" is defined ONLY by the env
value `COVERAGE_AUGMENTATION_TARGET` (`.env`; default 98) — never a hardcoded 100 or any
other inline constant. It is the single source of truth for the threshold.

**Decision:** The pipeline MUST NOT proceed to TASK 1 for any module until TASK 0c has
achieved at least `COVERAGE_AUGMENTATION_TARGET` percent line coverage for that module.
This is a hard block, not a warning. TASK 0c augments tests until coverage REACHES the
target; once coverage is at or beyond the target it MAY stop augmenting — i.e. stop ADDING
new tests (there is no benefit to chasing the residual llvm-cov-artifact / cfg-edge lines
past the target). Exception: proc-macro crates, where tarpaulin cannot measure coverage
(treated as meeting the target by convention).

**Tests are the oracle — preservation is UNCONDITIONAL.** "Stop augmenting once the target
is reached" means stop ADDING tests. It NEVER means dropping, skipping, or failing to keep
the tests that already exist. Every original `#[cfg(test)]` unit test AND every augmented
test MUST survive strip — extracted into kept `gen_tests` siblings — so it serves as the
reconstruction oracle in TASK 2/3. Skeletonise deliberately drops `#[cfg(test)] mod` from
the implementation scaffold (so it never stubs a test body); therefore those tests MUST be
preserved elsewhere FIRST. This preservation is NOT gated on whether augmentation adds new
tests: a module already at/above the target still has its existing tests kept. Silently
losing an original test shrinks the oracle and lets TASK 2 reconstruct wrong behaviour that
still "passes" — exactly the fraudulent-success failure Hard Rule 11 / FR-022 exist to
prevent. (Bug that motivated this: a module already at target had its inline unit tests
dropped at skeletonise because preservation was coupled to augmentation running.)

**Why:** TASK 1 extracts behavioral requirements from the test suite; TASK 2/3 reconstruct
and validate against it. Coverage below the target means code paths with no test → no
behavioral specification → thin/incorrect requirements → wrong implementations. The pipeline
is only as good as the oracle feeding it; any shortcut cascades into every downstream task.

**Implementation:** In the main module loop, after `task0c_augment_module()` returns, if
`cov < COVERAGE_AUGMENTATION_TARGET` and `crate_type != "proc-macro"`:
- Retry up to 10 times (clear the augmented sentinel, skip module, retry next outer pass).
- After 10 failed attempts: `sys.exit(1)` with a clear message.
Never proceed to strip or TASK 1 with coverage below `COVERAGE_AUGMENTATION_TARGET`. The
existing-test preservation invariant above holds regardless of the coverage value (including
when the module is already at/above target and augmentation is skipped).

---

## FR-019 — Whole-folder context principle: send everything, degrade by priority

**Decision:** Every TASK 0c per-file prompt must receive the maximum useful context from the
project. The default is to send the entire relevant folder: Cargo.toml, full source file,
all integration tests, all sibling source files, coverage strategy, attempt history.
When the total exceeds the context budget, content is dropped in a fixed priority order
(lowest-value first), and every dropped section is logged.

**Priority (highest = never dropped):**
1. Cargo.toml — defines what dependencies are importable; without it the model uses wrong crates
2. Full source file being targeted — the model needs every `use` statement and `impl` block
3. Uncovered function bodies with `!! UNCOVERED` annotations — the direct task
4. Public symbols extracted from the file — type signatures for correct construction
5. Coverage strategy (`coverage_strategy.md`) — import paths, type construction patterns
6. Prior attempt history — what not to repeat (BL-002)
7. Existing inline tests from the file — style reference
8. Project context — behavioral summary
9. Integration tests (`tests/*.rs`), shortest first
10. Sibling source files, shortest first

**Why:** The previous approach curated a subset of context by hand — extracted function bodies,
public symbols, sibling files. This bet that we know which parts are relevant. We don't. The LLM
does, but only if it can see everything. Sending the whole folder eliminates import-path guessing,
wrong type construction, and hallucinated dependencies — the three most common TASK 0c compile
failures. The context-guard wrapper in `ask()` is the last-resort safety net; the priority logic
above ensures it rarely triggers and that when it does, low-value content is trimmed first.

**Implementation:** `_build_task0c_prompt_sections()` implements this logic. Called per file in
`task0c_augment_module()`. Cargo.toml and integration test paths collected once per attempt loop
(not per file) to avoid redundant disk reads.

---

## FR-020 — Truncation must always happen at semantic boundaries, never at raw byte offsets

**Context:** Repeatedly, the pipeline introduces a size cap on some content (fn_bodies, source,
siblings, history) using a raw byte slice (`content[:N]`). This always eventually produces
broken output: a half-written function body with unclosed braces, a truncated import block,
a split struct definition. The LLM receiving broken syntax produces worse output than receiving
less content. This is a recurring failure mode that must be prevented by rule.

**Rule:** Every truncation in the pipeline must cut at a semantic boundary appropriate to the
content type:

| Content type | Truncation boundary |
|---|---|
| `fn_bodies` (function blocks) | Last `// --- next function ---` separator before the cap |
| Rust source / sibling files | Last complete line (`\n`) before the cap |
| Attempt history / strategy | Last complete paragraph or entry before the cap |
| Any structured list | Last complete item before the cap |

Raw `content[:N]` slices are forbidden unless N is guaranteed to be a line boundary.
Always append a `// ... (truncated)` marker so the LLM knows content was cut.

**Why:** A broken function body is worse than no function body. The LLM will try to make sense
of unclosed braces and produce syntactically invalid test code. Less complete content is always
preferable to structurally invalid content.

**Implementation:** `_build_task0c_prompt_sections()` uses `rfind` on the separator string to
find the last clean boundary before the cap. Any new truncation site added to the pipeline must
follow the same pattern. Code review should flag any `[:N]` slice on multi-line content.

---

## FR-021 — Do NOT wrap gen_tests blocks with GRCOV_EXCL_START/STOP markers

**Context:** GRCOV_EXCL_START/STOP markers were added (session 28) to exclude gen_tests blocks
from the grcov denominator, on the theory that test code shouldn't count toward source coverage.
This caused two compounding problems:

1. The gen_tests source lines were excluded from both numerator and denominator — removing the
   numerator contribution from running tests.
2. LLVM phantom artifact lines (profraw entries with line numbers beyond the actual file length,
   generated by the compiled gen_tests binary) were NOT excluded — they stayed in the denominator
   as permanently uncovered. No test can ever cover them.

The result: 100% coverage is structurally unreachable with EXCL markers. The denominator always
contains phantom lines that can never be hit.

**Rule:** Do not wrap gen_tests blocks with GRCOV_EXCL markers. The pipeline-level filter
(`l <= _src_line_count`) already prevents the LLM from targeting lines inside gen_tests blocks —
gen_tests are always appended at the end of files, so their line numbers exceed the stripped
file length and are filtered before reaching the LLM prompt. EXCL markers are not needed for
correct LLM targeting.

**Without EXCL markers:**
- gen_tests lines run and get coverage hits → contribute to numerator
- Phantom artifact lines at those positions also get hits → reduces phantom drag
- 100% is reachable (reached 97.2% in session 29 via this mechanism)

**Why this is safe:** The LLM never sees gen_tests lines as coverage targets regardless of EXCL,
because `uncov_lines` is filtered to `l <= stripped_file_length` before building the prompt.

---

## FR-022 — IMPORTANT! No access to original source after strip. NO EXCEPTIONS.

**Decision:** From TASK 1 onwards, no pipeline code path may read the original
implementation source. The original is read exactly once, by
`_bake_skeleton_artifacts`, which runs between task0c and strip on the live source
tree. That step writes every artefact downstream phases will need (skeleton with
`unimplemented!()` bodies, fn-name reference counts, cluster manifest, API surface)
to disk. After strip, those artefacts plus tests plus stripped source are the only
inputs.

**Why this matters more than any other rule:** The entire experiment is
"can the LLM reconstruct a library from tests + signatures alone?" If pipeline
code can re-read the original bodies post-strip — even once, even for the hardest
fns, even behind a feature flag — then we are measuring "LLM copies an answer"
not "LLM reconstructs from tests". Every coverage number, every fn fill, every
note becomes meaningless. The rule is absolute because anything less reduces
the project's worth to zero.

**Forbidden behaviours (non-exhaustive):**
- Re-reading any `.rs` file under the original `src/` after strip
- Checking out the `task0c-augmented-{module}` git tag from pipeline code
- Scanning `/tmp/prestrip*` or any directory containing pre-strip source
- Injecting pre-strip bodies into LLM prompts as "fallback" or "last attempt"
- Adding a feature flag that conditionally enables any of the above
- Caching pre-strip content in memory across the strip boundary
- Adding a new bake-step output that contains pre-strip implementation bodies
  (only signatures, types, macros, and reference counts may flow through)

**Allowed:** Humans may `git checkout task0c-augmented-{module}` to inspect by
hand. Pipeline code may not.

**Implementation:**
- `_bake_skeleton_artifacts(project_dir, module_name)` — the only legitimate
  reader of live source. Runs in the main module loop between
  `_capture_api_surface` and `strip_module_source`.
- `_load_baked_clusters`, `_load_baked_leaf_order` — post-strip consumers of
  the bake artefacts. Read only `.skeleton/_clusters.json` and
  `.skeleton/{cluster}/_leaf_order.json` respectively.
- `_checkout_prestrip_source` and `_find_prestrip_commit` were deleted. They
  must not be re-added. Any future feature claiming to need them is wrong by
  construction — extend the bake step instead.
- `tools/skeleton_extractor` skip-list includes `.skeleton*`, `.phase_c_logs`,
  `.original_structure` so it cannot accidentally walk pipeline scratch dirs
  even if a caller misroutes the input path.

**When a fn cannot be reconstructed:** Accept the failure. Mark it
`// SKELETON_STUB_FAILED_ATTEMPT`. Improve the prompt or the architecture
on the next iteration. Do NOT relax this rule.

**Cross-reference:** CLAUDE.md Hard Rule 11 states the same constraint in
operational terms. The two must stay in sync — when one is updated, update
the other.
---

## FR-023 — gen_tests sibling files are pure test code and must survive strip verbatim

**Decision:** Strip MUST preserve `*.gen_tests_N.rs` sibling files in `src/`
byte-for-byte. They are pure test code generated by task0c against the
original implementation — every line is signal for reconstruction. Strip
must NOT pass them through `extract_rust_tests` (which keeps only items
annotated `#[test]`/`#[cfg(test)]` and drops everything else).

**Why this matters:** A `gen_tests_N.rs` sibling is structured as:
```
use super::*;                                  ← required imports
use std::io;                                   ← stdlib types referenced in tests
#[derive(Debug)] struct MyTestErr(...);        ← helper type
impl Display for MyTestErr { ... }             ← helper impl
impl StdError for MyTestErr {}                 ← helper impl
#[test] fn test_from_boxed() { ... MyTestErr ... io::Error ... }
```

If strip runs `extract_rust_tests` on this file, it keeps ONLY the `#[test]`
items and drops imports + helper struct + helper impls. The resulting file
references `MyTestErr` and `io::Error` without declaring or importing them.
Result: the gen_test fails to COMPILE the moment Phase C tries to run it.
The TDD compile-frontier reports unresolved-name errors that don't map to
any reconstruction target (the missing symbol is in the test file itself),
so the driver has nothing to act on. The gen_test is silently disabled —
present on disk, never executed, never producing the panic signal that
would drive a fill.

**Required behaviour:**
- `strip_to_tests.py`: recognise filenames matching `*.gen_tests_N.rs` and
  copy them through verbatim (treat them the same as files inside `tests/`).
- Same applies to any other pipeline scratch test files generated under
  `src/` for the same reason (e.g. future `*.spec_tests_N.rs`).
- The recognition is by filename pattern, not by content inspection —
  content-based heuristics drift; the filename convention is invariant.

**Forbidden behaviours:**
- Running `extract_rust_tests` on `*.gen_tests_N.rs` siblings.
- Re-generating gen_tests from scratch post-strip (would need original
  source access — see FR-022).
- "Lightweight" stripping that drops only `pub use` re-exports, or only
  doc comments, etc. Anything less than verbatim preservation drifts.

**Implementation anchor:** The recognition check goes in `process_rust` in
`strip_to_tests.py`, alongside the existing `is_rust_pure_test_file` check.
Regression test: a fixture file with imports, helper struct, and a `#[test]`
fn must survive strip byte-identical when its name matches `*.gen_tests_N.rs`.

**Cross-reference:** This rule complements the propagation step in
`StaticSkeletonTask2Generator` (pipeline.py) which copies gen_tests from
`project_dir/src/` into `.skeleton-filled/{cluster}/src/`. Propagation
assumes the sources are intact; strip MUST keep them that way.


---

## FR-024 — Every cargo test invocation runs in an improvement-driven loop

**Decision:** Every place in the pipeline that runs `cargo test` MUST wrap
it in an improvement-driven loop. Single-shot invocations and hard-capped
retries are forbidden. The loop metric is the parsed `(pass_count, fail_count)`
from cargo's `test result:` lines; the loop iterates as long as `pass_count`
strictly increases OR new fns get filled; it exits only after `max_stalls`
(default 3) consecutive non-improving iters. Hard caps are retained ONLY
as runaway safety nets, set well above expected iter counts.

**The full attribution chain (in order):**

1. **Runtime panics** — `_extract_tdd_frontier` maps `panicked at file:line:col`
   to specific fn records via the syn extractor.
2. **Compile errors** — `_extract_tdd_compile_frontier` walks `--> file:line`
   markers back to enclosing fns.
3. **LLM classifier** — `_classify_phase_c_test_failures` (P4.5). One call
   classifies failing tests into BAD_FILL / TEST_PRECONDITION_MISSING / etc.
4. **Recently-filled fallback** — ring buffer of the last N fns filled.
   New assertion failures are most likely caused by recent fills.
5. **Test-reverse-map fallback** — static analysis: for each `#[test] fn name`
   in test files, the set of fn records whose identifiers appear in the
   test file's text.

If all five paths produce zero actionable fns AND the stall budget is
exhausted → exit. Otherwise continue.

**Sites that MUST follow this rule:**

- `_run_phase_c_tdd` (TASK 2 per-cluster TDD loop)
- `_post_merge_fill_pass` (post-merge fill on the merged tree)
- `_task3_inline_retry` (TASK 3 retry path)
- Any future task that invokes cargo test

**Forbidden:**

- `for iter_n in range(MAX):` without a stall budget inside.
- Exiting on `frontier_empty` alone, ignoring the stall counter.
- Treating a single classifier `no_actionable_refs` as terminal — fall
  back to recently_filled and reverse_map first.
- Using a numeric retry cap as the PRIMARY exit gate.

**Implementation anchors:**

- `_parse_test_counts(output)` — the metric parser.
- `_build_test_reverse_map(test_files, src_dir)` — static analysis for
  the reverse-map fallback.
- `_attribute_failures(...)` — full attribution chain in one call.
- Each loop maintains its own recently-filled ring buffer over the last
  N iters (per-loop state, not global).

**Cross-reference:** This is the test-driven counterpart to the
compile-error `_improvement_loop` pattern. Both share the principle:
numeric caps are a smell; improvement is the criterion.

## FR-025 — Cross-cluster merge identity for impl blocks must be the full impl header

**Decision:** The syn-based cluster merger (`tools/skeleton_extractor`,
`merge_cluster_files`) groups same-identity items across clusters before
emitting cfg-gated copies of divergent ones. For `Item::Impl`, the identity
key MUST be the **complete impl header** — impl generics, the trait reference
*including its generic arguments*, the self-type *including its generics*, and
the where-clause. Two impls whose headers differ in ANY of these are distinct
blocks and must never be merged into one.

**Why:** The previous key was `path_to_string(trait) | quote(self_ty)`, and
`path_to_string` discards generic arguments (it joins segment idents only).
This collapsed structurally distinct impls that share a trait name and self
type:

- `impl AsRef<dyn StdError> for Error` and
  `impl AsRef<dyn StdError + Send + Sync> for Error` → same key → merged into
  one impl holding two `as_ref` fns with different return types → **E0201**
  (duplicate method) + **E0053** (incompatible return type).
- `impl<'a,T> Mut<'a,T>` (holds `read(self) -> T`, T implicitly Sized) and
  `impl<'a,T> Mut<'a,T> where T: ?Sized` (pointer methods) → key ignores the
  where-clause → merged → `read` lands under `where T: ?Sized` → **E0277**
  (unsized return).

Both are unrecoverable downstream: the fix loop is forbidden from changing
signatures or impl headers (FR — body-only fixes), so it churns forever on
`as_ref` (splice_rejected) and `read` (fn_not_found). The skeleton was always
correct; only the merge corrupted it. Fix the process (merge identity), never
the symptom (LLM body edits).

**Rule:** impl identity = `quote(generics) + trait_ref(with args) + quote(self_ty) + quote(where_clause)`.
This is the maximally-constraining identity (Hard Rule 9): when two impls might
be "the same", the merge must add MORE constraints to keep them apart, not
fewer to fuse them.

## FR-028 — Retrospective/diagnosis calls run with think=True, output-bounded

**Decision:** Every retro/diagnosis LLM call (`_phase_c_retrospective`,
`_whole_tree_retrospective`, and any future diagnosis prompt) MUST use
`think=True` via the shared `_ask_retrospective` helper, with a bounded OUTPUT
budget (`_RETRO_MAX_TOKENS`) and one retry on empty content. `think=False` is
forbidden for these calls.

**Why:** The served `Qwen/Qwen3.6-27B-FP8` (vLLM, `--reasoning-parser qwen3`)
is a hybrid-thinking model. Under `think=False` it returns EMPTY `content` — the
reasoning path never populates the visible answer — so the retro silently
no-ops and the fix loop loses its guidance at exactly the stall points where it
needs it most. Fill calls already use `think=True` and work. think=False was
chosen for speed and to avoid context-budget blowups, but those blowups were on
large fill/whole-file prompts; retro INPUT is tiny (error excerpt capped at a
few KB), so the correct lever is bounding OUTPUT tokens, not disabling thinking.

**Rule:** retro = `think=True` + `max_tokens=_RETRO_MAX_TOKENS` + retry-on-empty.
The output cap must leave room for reasoning AND the final answer to both emit —
if reasoning alone exhausts the budget, `content` truncates to empty again, so
the cap is generous (not a 600-style starvation cap, which FR-024 already
rejected).

## FR-026 — Doc comments are condensed (not stripped, not dumped) in fill views

**Decision:** In the prompt view shown to the model for body fills, doc
comments are CONDENSED to a single `// <intent>` (or `//! <intent>`) line via
the deterministic Python `_condense_doc_attrs` — never dumped verbatim and
never erased. Each run of `#[doc=...]` / `#![doc=...]` collapses to the first
sentence of prose; fenced code examples, badges (`[![…]]`), link refs, and
inline `[t](url)` decorations are dropped (the url, not the text). The on-disk
file is untouched.

**Why:** Two prior behaviours were both wrong. Dumping raw docs floods the
prompt (anyhow embeds hundreds of chars of example code per public fn — it hit
the 100%-budget guard). Stripping docs to nothing throws away the author's
intent, which is high-signal exactly for the hard fns (e.g. the `AdhocKind`
specialization trick, the `Context` blanket impl). Condensing keeps the intent
at ~1 line, in place (locality — the model fills that fn and sees its purpose
right there), at negligible token cost (~46% smaller on error.rs).

**Deterministic, not LLM (Hard Rule 9):** condensation is pure text processing
on skeleton-derived `#[doc]` attrs (a pre-strip artifact, Rule-11-clean). No
model call — the summary must be reproducible.

**Scope / follow-ups:** the per-fn fill view (`_annotate_file_view`) uses it
now. `#[cfg_attr(.., doc=..)]` conditional docs are intentionally left intact
(cfg machinery, low volume). Still TODO: a 1-line `// intent:` per stub in the
per-impl render, and a compact `file::sym → summary` doc-index artifact for the
overview-style prompts (TASK 0b) that still dump full files.

## FR-027 — Autoref-specialization dispatch families are co-designed as one unit

**Decision:** A "dispatch family" — 2+ sibling traits declaring the same method
name, where that method is invoked (`.method(...)`) from a `macro_rules!` body —
is detected statically by the syn extractor (`--enumerate-dispatch-families`)
and co-designed together in the per-impl fill. When a block implements a family
trait, the fill broadens `related_blocks` to EVERY family-trait block and
injects a `<DISPATCH_FAMILY>` section carrying the dispatching macro source (the
behavioural spec). Detection is AST/token-walk only — no regex (banned).

**Why:** anyhow's `anyhow!`/`ensure!` use autoref-specialization:
`AdhocKind`/`TraitKind`/`BoxedKind` each expose `anyhow_kind` returning a
distinct tag, and the macro's `x.anyhow_kind()....` chain dispatches by method
resolution over the blanket-impl bounds. Correctness is a CROSS-IMPL invariant:
filling one member in isolation (the model returned just `Boxed` for
`anyhow_kind`) silently breaks every call site — `anyhow_kind` *compiled* but 14
runtime tests failed. Per-fn / per-impl granularity is structurally too small
for this class; the family must be seen and designed together.

**Detection signal (general, not anyhow-specific):** shared method name across
sibling traits, GATED by an actual macro that calls it. The macro gate is what
excludes incidental shared names (e.g. std's `source`). On anyhow it cleanly
finds exactly two families (`anyhow_kind`, `__dispatch_ensure`) and nothing
spurious.

**Approach:** co-design is delivered by surfacing the whole family + the
dispatch macro as shared context to each member fill (reusing the per-impl
loop), not by a single mega-call — the model gets the full family view on every
member so it keeps tags and bounds consistent. If it still fails with the full
family + macro spec shown, that fn class is genuinely hard (Rule 11 spirit):
accept the limit, do not cheat.

**Rejected earlier framing:** "macros are a missing fill unit." The spike showed
macros are preserved verbatim in the skeleton — not the problem. The defect is
the macro's SUPPORT machinery (the specialization traits), so the fix is family
co-design, not macro reconstruction.

**B1 — chain-following (the family surface is the whole macro call chain).**
The dispatched method is only the FIRST hop. anyhow's macro chains
`x.anyhow_kind().new(arg)`: `anyhow_kind` (a trait method) returns a tag type
(`Adhoc`/`Trait`/`Boxed`), then `.new(...)` is called on that tag — and `new`
lives on the tag's INHERENT impl, not on any trait. A trait-only scan therefore
sees `anyhow_kind` (trivial: return a tag) but misses `new` (the method that
actually builds the `Error`), which is exactly where behaviour was wrong while
the code still compiled.

The detector follows the chain: for the dispatched method it reads its return
types from the trait signatures, finds `.first().second(...)` chains in the
macro body (token walk, no regex), and keeps `second` when it exists on a return
type's inherent impl. The family then carries `return_types`, `chained_methods`,
and `member_files`. The fill treats a block as a family member when its
container names a family trait OR a return ("tag") type, so the tag impls
(holding `new`) are co-designed with the dispatch traits, and the prompt spells
out the full `.method().chained()` path. General signal — no crate-specific
names. Open follow-ups: fill the whole family in ONE call (B2) and stop
reverting locally-correct fills blocked by other unfilled fns (B3, FR-029).

## FR-029 — Keep non-regressing fills (dissolve the convergence deadlock)

**Decision:** A per-impl fill is accepted when it does NOT regress — it adds no
compile error beyond the block's baseline (the crate's errors with this block's
stubs still unimplemented) AND no remaining error points at the filled fns —
even if the crate as a whole does not yet compile. The old rule (`rc==0`, whole
crate compiles, else revert) is replaced by "revert only on a real regression."
Gated by `PHASE_C_KEEP_NONREGRESSING` for instant rollback.

**Why:** Interdependent code (anyhow's macro-support traits: `Bool`, the `ensure`
machinery, `Context`, the `anyhow_kind` family) has a stable error set no single
fill clears alone. Under the `rc==0` gate every CORRECT piece is reverted for not
single-handedly making the crate compile, so prerequisites never accumulate and
the error floor never drops — a convergence deadlock. The B3 shadow run proved
it: 16 fills with zero errors on themselves (e.g. `anyhow_kind` returning its
tag) were being discarded purely because ~14 errors sat elsewhere.

**Guard (never relax blindly):** keep ONLY when the post-splice error set adds
nothing over baseline and nothing references the filled fns. A fill that
introduces any new error, anywhere, is still reverted. A kept fill that compiles
but is behaviourally wrong is caught downstream by the TDD test loop (FR-024) —
B3 unblocks compile convergence; it does not bypass behavioural validation.

**Validation:** live KEEP/REVERT decisions must match the shadow predictions, and
the per-pass error floor should drop below its previously-stuck value for the
first time as correct pieces accumulate.

## FR-030 — Clusters that gate a nightly feature must use the nightly toolchain

**Decision:** At cluster identification, a compiler cluster is marked
`requires_nightly=True` (→ `toolchain="nightly"`, so every per-cluster compile
and TASK 3 runs `cargo +nightly`) when its active cfg-set intersects the set of
cfgs that gate a nightly `#![feature(...)]` attribute. That set is computed
statically by the syn extractor (`--enumerate-feature-gate-cfgs`): it finds
`#[cfg_attr(COND, feature(...))]` and returns COND's leaf cfg predicates. No
regex.

**Why:** anyhow's nightly cfg activates
`#![cfg_attr(error_generic_member_access, feature(error_generic_member_access))]`,
a nightly-only attribute. Running that cluster on stable yields E0554. The old
behaviour relied on a runtime E0554→nightly retry that did not persist to the
cluster manifest, so TASK 3 still ran on stable. Static detection at cluster ID
is deterministic and persists.

## FR-032 — Cluster gate predicate must OR the cfg and feature forms

**Decision:** When the merge gates a divergent item to one cluster
(`_cluster_cfg_predicate_for_gate`), each active predicate `N` becomes
`any(N, feature = "N")` and each excluded predicate becomes
`not(any(N, feature = "N"))`, all AND'd. A cfg can be switched on EITHER as a
bare `--cfg N` (RUSTFLAGS) or as a cargo `feature = "N"`, and a cluster sets
only one; the gate must be true under whichever form that cluster uses.

**Why:** The old builder read `cfg_true`/`cfg_false`, which list BOTH forms as
separate AND terms → `all(N, feature = "N", …)`. A `--cfg`-activated cluster
does NOT enable the feature, so that gate was UNSATISFIABLE, and every divergent
method gated this way disappeared under that cluster — producing E0046 ("not all
trait items implemented") across the merged tree for the non-stable clusters,
even though each cluster's source compiled standalone. `cfg_true`/`cfg_false`
remain correct for predicate EVALUATION (OR semantics); only the gate BUILDER
was wrong. Verified: with the fix, the merged anyhow lib compiles under all
three clusters (stable / anyhow_no_core_error / nightly) with 0 errors.

## FR-033 — Doc comments merge to one-line `#[doc]` so rendering stays uniform

**Decision:** Before the merged tree is unparsed (`merge_cluster_files`), every
multi-line `#[doc = "...\n..."]` is split into one single-line `#[doc]` per
line. prettyplease renders single-line docs uniformly as `///` / `//!`, but
switches any doc string containing an embedded newline to the `/** */` / `/*! */`
block form.

**Why:** A doc example that ends up rendered with a MIX of `///` and `/** */`
splits the example's ```` ``` ```` code fence across two comment kinds, so its
hidden setup lines (`# struct SuspiciousError;`, `# fn helper() {...}`) no longer
reach the doctest — yielding E0425 ("cannot find type/function") on otherwise
correct examples. The same alternation corrupted module docs (the earlier bare
`//!` line). Normalising to one line per `#[doc]` keeps prettyplease in the
uniform `///`/`//!` path, so doctests and module docs render intact. Verified:
0 block-doc lines in the merged `lib.rs`, examples preserved.

## FR-036 — Reconstruction target is test-passing behaviour, not source fidelity

**Decision:** The pipeline reconstructs ANY implementation that compiles and
passes the tests. It is NOT trying to reproduce the original library's
implementation, and it must never infer the original's implementation OR its
strategy/approach. No phase, prompt, or autopilot action may steer the model
toward "what the upstream library does" (e.g. "use `Box` because anyhow does",
"the real vtable is laid out like X"). The model is free to invent its own
scheme; the pipeline only helps that scheme COMPILE by surfacing real, in-scope,
stable symbols.

**Why:** The whole experiment is "reconstruct from tests," so the success
criterion is the test suite, not byte- or strategy-level similarity to the
source. Concretely, when the model wrote `core::alloc::Global.allocate_zeroed(...)`
instead of `Box`, that was not "wrong because anyhow uses Box" — a raw-allocator
scheme is a perfectly valid reconstruction. It failed only because the SYMBOLS
don't exist/aren't stable (`Global` is in `alloc::alloc`/`std::alloc`;
`allocate_zeroed` is unstable). Forcing the anyhow approach would (a) leak
source-derived strategy and (b) discard valid alternative implementations.

**Allowed inputs to any fix:** the tests; the frozen signatures/skeleton; the
in-scope symbol inventory; the cluster's stable-API constraints (toolchain/cfg);
the model's own compile/test-error feedback (incl. anti-hallucination notes).
**Forbidden:** the original bodies (already FR-022/Hard Rule 11), AND any
"upstream implements it as X" inference — strategy, layout, type choice, or
algorithm derived from the original. This FR extends FR-022 from "no source
bytes" to "no source-derived strategy."

**Consequence for the vtable/alloc failure class:** the fix is NOT "tell it to
use Box." It is (1) co-design the coupled vtable/`Own`/`Ref`/`construct_*` unit
in one prompt so the model's own symbols stay self-consistent
(`ADHOC_VTABLE` vs `VTABLE_ADHOC` is self-contradiction, not missing knowledge);
(2) surface the available in-scope + stable symbol inventory so it stops
inventing non-existent paths; (3) let compile errors + the model's notes correct
symbol locations. Whatever scheme it then converges on is its own choice.

## FR-037 — No target retrospective for downstream-only failures

**Decision:** `_phase_c_retrospective` returns immediately
(`signal=downstream_only`, no LLM call) when the focused error excerpt has
errors ONLY "elsewhere in the crate" and none "in target file".

**Why:** When a fn compiles fine but the crate is broken by ANOTHER fn (or a doc
parse error), the fix loop still ran a retrospective on the innocent target —
which misattributed the cause (e.g. blaming `construct_from_boxed`, which exists
and is surfaced, for a `lib.rs` doc parse error) and wrote a WRONG
anti-hallucination note that then misleads future attempts. The fix-loop already
classifies errors target-vs-elsewhere (`_focus_cargo_errors`); the retro just
ignored it. Found via the night's scientific pass: the cited "API hallucination"
failures were largely this misattribution, not real invention. B3 (FR-029)
already keeps non-regressing fills, so an innocent fn is rarely reverted anyway —
this stops the wrong notes.

## FR-038 — gen_tests are reattached per-cluster (content+gate never separate)

**Decision:** The post-merge gen_tests reattach sources tests PER-CLUSTER from
`.skeleton-filled/{cluster}/src/`. Tests identical across clusters are emitted
once, ungated. Tests that diverge across clusters are emitted as one gated
sibling per cluster, with a cluster-unique filename (`foo.gen_tests_N_{cluster}.rs`,
mod `gen_tests_N_{cluster}`), gated by that cluster's `_cluster_cfg_attr`.

**Why:** The old reattach keyed `saved_gen_blocks` by file path and read from a
single collapsed tree, so when clusters had a same-named gen_test with divergent
content (e.g. nightly's `request_ref` vs no_core's), one CONTENT won while the
cross-cluster merge set a different GATE — yielding nightly `request_ref` content
under a no_core gate → E0658, breaking the no_core test binary. Sourcing
per-cluster keeps each test's content and its required cfg together; a nightly
test is gated nightly and excluded from stable/no_core. Validated via the night's
spike (nightly tree gates nightly, no_core tree gates no_core — each correct
alone; only the merge mixed them).

## FR-039 — Mutation-kill behaviour hardening in task0c (M1–M4)

**Decision:** task0c's strength metric is augmented from line coverage to
mutation-kill. Pipeline: M1 `_mutation_survivors` (cargo-mutants, per-cluster
toolchain/env → surviving mutants); M2 `_mutation_kill_check` (attach a
candidate test, run the real suite, re-run mutants, confirm the survivor moved
missed→caught, restore the file); M3 `_mutation_harden_file` (LLM proposes
BLACK-BOX behaviour tests from the PUBLIC API only — never source or mutant
diffs (FR-036) — kept only if they compile, pass, and lower the survivor count);
M4 `_task0c_mutation_pass` persists kept tests as per-cluster gen_tests siblings
(FR-038). Coverage stays as the cheap reachability floor.

**Why:** coverage-driven tests are one-liners that execute a line without
asserting its effect, so post-strip reconstruction is under-constrained.
Mutation-kill defines strength as "catches a behaviour change," directly
rejecting no-assert tests. Spike-validated on anyhow (cargo-mutants: 176
mutants, ~2.4s each; real survivors like `>→>=` in `Indented::write_str`
reachable via the public Debug path).

**Opt-in:** gated by `TASK0C_MUTATION_HARDEN=1` (default off) until validated on
a full run; M1/M2 are spike-validated, M3/M4 need a real run. Dep: cargo-mutants
(setup.sh).

## FR-040 — Undefined-symbol guidance for the vtable / shared-machinery class

**Decision:** A fix-loop gotcha (auto-injected by `_gotchas_for_error` on
`E0425`/`E0433` / "cannot find value|function" / "in this scope") tells the
model: you fill fn BODIES only — you may NOT add a new top-level item (`static`,
`const`, free fn, `use`) — so a referenced-but-undefined module-level symbol can
never resolve. Build what you need INLINE from in-scope items; if several fns
need the same value, build it the same way in each (there is nowhere to define a
shared external name); do not guess paths like `core::alloc::Global` — use an
in-scope item (e.g. `Box`). A detector `_coupled_cofill_groups` identifies the
situation (2+ fns in a file sharing "cannot find" errors).

**Why:** anyhow's type-erasure (`Own`/`Ref`/`ErrorVTable`) led the model, filling
`construct_*`/`new` per-fn, to invent a shared vtable static under inconsistent
names (`ADHOC_VTABLE` vs `VTABLE_ADHOC`) that no fill could define — E0425 across
the construction machinery. The real constraint is structural: body-only fills
cannot introduce the top-level static, so the value must be built inline. This is
source-free (FR-036): it teaches the constraint and points at in-scope/stable
symbols, never anyhow's actual implementation or "use Box because anyhow does".
