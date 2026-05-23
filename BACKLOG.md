# Pipeline Backlog

Please refer to plan.md for the execution plan.

Items are ordered by priority. Each entry has a status: `open`, `in-progress`, `done`.

---

## BL-001 — TASK 0b: Discovery pass before coverage augmentation
**Status:** done
**Context:** TASK 0c currently receives piecemeal context per file. The LLM has no upfront understanding of the project's test patterns, type construction idioms, or import conventions.
**What was built:**
- `task0b_discovery()` function that runs once per project before TASK 0c
- Passes: all `src/*.rs`, all `tests/*.rs`, `Cargo.toml`
- LLM produces `coverage_strategy.md`: correct `use` import paths, type construction patterns, cfg/feature flags, test style examples
- Output stored to disk, invalidated by content hash
- `coverage_strategy.md` prepended to every TASK 0c per-file prompt, TASK 1 prompts, and TASK 2 prompts

**Expected gain:** LLM stops guessing import paths and type constructors; fewer compile failures per file.

---

## BL-002 — Per-file failure/success log passed into TASK 0c
**Status:** done
**Context:** Each TASK 0c attempt starts completely fresh. If attempt 1 wrote `assert_eq!(x, [])` and got a type error, attempt 2 makes the same mistake.
**What was built:**
- Append-only JSONL store per project: `.task0c_attempts.jsonl`
- Entry format: `{file, attempt, block_preview, compile_error, outcome, ts}`
- Before each per-file LLM call: retrieve all prior attempts for that file, format as "what not to repeat"
- Both failures and successes logged — gives the model negative AND positive examples

**Expected gain:** Iterative self-correction within a file; avoids repeating known-bad patterns.

---

## BL-003 — Full source file in TASK 0c prompt (not just uncovered function bodies)
**Status:** done
**Context:** LLM previously saw only extracted function bodies + public symbols. Module-level `use` imports and surrounding `impl` blocks were invisible — common cause of wrong import paths in generated tests.
**What was built:** Full source file content passed as a top-level section in the prompt (alongside the annotated uncovered functions section).

---

## BL-004 — Integration test files (`tests/*.rs`) in TASK 0c prompt
**Status:** done
**Context:** `_extract_existing_tests` only pulled inline `mod tests` blocks. The real test suite (`tests/*.rs`) — which already compiles and passes — was never shown.
**What was built:** All `tests/*.rs` files collected and passed to the TASK 0c prompt (subject to BL-007 budget priority).

---

## BL-005 — Fix prompt re-sends source context (currently blind)
**Status:** done
**Context:** When compilation fails and a fix is requested, the fix prompt contained only the compile error and the broken block. No types, no imports, no source file — the LLM fixed blind.
**What was built:** Fix prompt now includes Cargo.toml, full source file, public symbols, and existing inline tests alongside the compile error.

---

## BL-006 — SYS-1: Replace hand-rolled Cargo.toml parser with `cargo metadata`
**Status:** done
**Context:** Current dep graph scanner is fragile (caused phantom `anyhow_test` module bug). `cargo metadata --format-version 1 --no-deps` is authoritative.
**What was built:** `StaticDependencyGraph._parse()` now tries `cargo metadata` first via `_try_cargo_metadata()`, falls back to the regex parser only if cargo metadata fails (no cargo binary, offline environment, etc.).

---

## BL-007 — Prioritized degradation when folder doesn't fit context window
**Status:** done
**Context:** The whole-folder approach (BL-003 + BL-004) sends everything to the LLM. For large projects this may exceed the context budget.
**Design: "fit → send all, else → degrade by priority"**

When total context fits in `SINGLE_CALL_CHAR_LIMIT`:
- Send everything: Cargo.toml, full source file, uncovered function bodies, public symbols, coverage strategy, attempt history, existing inline tests, project context, ALL integration tests, ALL sibling source files.

When total exceeds budget, drop in this order (lowest priority first):
1. Keep always: Cargo.toml + full source file + uncovered fn bodies + public symbols (~5-25k chars). These are non-negotiable — without them the LLM cannot write correct tests.
2. Add if fits: coverage_strategy (capped at 4000 chars), attempt_history, existing inline tests, project context.
3. Add if fits: integration tests, shortest first. Dropped ones are logged.
4. Add if fits: sibling source files, shortest first. Dropped ones are logged.

The context-guard wrapper in `ask()` is the last-resort safety net but should rarely trigger if the above budget logic is correct.

**What was built:** `_build_task0c_prompt_sections()` helper implements prioritized assembly. Dropped sections are logged so the autopilot can detect when context is being cut.

---

## BL-010 — TASK 0b: signatures-only condensation for source input
**Status:** open
**Context:** TASK 0b only needs to know import paths, type shapes, cfg flags, and available macros — not function bodies. Currently it sends full source, which puts it 14% over the FILE_CHAR_LIMIT for anyhow and will blow the limit entirely on larger projects. Files that exceed the budget are silently dropped (see BL-017), meaning the strategy doc can be built from an incomplete picture.
**What to build:** Extract a condensed representation of each source file — `pub struct`/`enum`/`trait`/`fn` signatures plus type definitions, stripping all function bodies. Can use `grep-ast` (the library Aider uses) for tree-sitter-based extraction, or a simpler regex pass over `pub fn`/`pub struct` lines. Feed condensed source to the TASK 0b prompt instead of full bodies.
**Expected gain:** Eliminates TASK 0b context overrun on all projects. Makes silent drops impossible because condensed signatures always fit.

---

## BL-011 — Repomix integration
**Status:** done
**Context:** Repomix compresses a whole repo into a single AI-optimized XML/Markdown file. Evaluated as a possible input for TASK 0b or TASK 0c.
**Why not needed:** Our pipeline already does structured file reading in Python with explicit priority control (BL-007). Repomix adds a subprocess dependency, loses the priority system, and is designed for single-shot interactive use — not automated per-task prompts. No gain over reading files directly.

---

## BL-012 — LlamaIndex / RAG vector store
**Status:** done
**Context:** LlamaIndex could index source files into a vector database for semantic retrieval — feeding only relevant snippets to each LLM call instead of whole files.
**Why not needed:** At anyhow scale (~12 source files) all context fits inline. The structured SQLite store (BL-021) covers the cross-task knowledge retrieval need without a vector DB. Decision recorded in project memory: defer vector DB until multi-project corpus exists.

---

## BL-013 — Aider interactive tool integration
**Status:** done
**Context:** Aider uses a "Repo Map" (signatures only) plus just-in-time full file reads to stay within context limits. Evaluated as a possible way to manage context in TASK 0c.
**Why not needed:** Aider is an interactive human-in-the-loop CLI tool, not a library. Our pipeline is headless and automated. The concepts behind Aider (signatures-only map for discovery, Planner+Worker for relevance selection) are worth implementing directly — see BL-010, BL-014.

---

## BL-014 — TASK 0c: Planner+Worker sibling selection
**Status:** done
**Context:** Currently TASK 0c includes sibling source files sorted by size (shortest first, BL-007). This is the wrong heuristic — a large file that defines the types directly used by the target function is more valuable than five small files that aren't referenced at all. We cannot compress siblings to signatures-only because writing behavioral assertions requires understanding what functions actually do.
**What to build:** Add a Planner call before the test generation call. The Planner receives the repo map (signatures only, from BL-010 logic) and the target function. It returns a short list of sibling files that are actually referenced. Only those siblings are included in the Worker (test generation) call in full. Replaces the current shortest-first heuristic.
**Expected gain:** Sibling context is relevant instead of arbitrary. Budget used on files that matter, not on unrelated utilities.

---

## BL-015 — `ask()` context guard: section-aware trimming
**Status:** done
**Context:** The context guard in `ask()` trims the middle 62.5% of the prompt body when the total exceeds MODEL_CONTEXT_CHARS. It does this blindly — it may cut through the middle of a high-priority section while leaving low-priority tail content intact. BL-007 already assembles prompt sections in explicit priority order but the guard ignores that structure.
**What to build:** Instead of trimming the middle of the buffer, the guard should identify section boundaries (using the `<FILE>`, `// ---`, and similar markers already present) and drop whole sections in reverse priority order (siblings → integration tests → history → strategy) until the prompt fits. Only fall back to middle-trimming if no whole section can be dropped.
**Expected gain:** Dropped content is always the lowest-priority section, not a random slice of the most important one.

---

## BL-016 — TASK 0c: improve attempt history block preview
**Status:** done
**Context:** `.task0c_attempts.jsonl` stores 300 chars of the failed test block and 500 chars of the compile error per attempt. For complex compile errors (type mismatches spanning multiple lines, missing trait bounds), 500 chars truncates mid-message and the model on the next attempt doesn't understand what failed. The 300-char block preview is too short to show even one full test function.
**What to build:** Replace raw truncation of the failed test block with a signatures-only compression (same approach as BL-010): extract only the `fn test_*` function signatures from the failed block, dropping the bodies. This typically reduces a 2000-char block to ~200 chars, freeing the saved budget for the full compile error (uncapped).
**Expected gain:** Model on attempt N+1 sees the full compile error from attempt N, not a truncated version. Reduces repeat mistakes in the fix loop.

---

## BL-017 — TASK 0b: warn when source files are silently dropped
**Status:** done
**Context:** When source or test files exceed the per-section budget in TASK 0b, they are dropped silently. The coverage strategy is then produced from an incomplete picture of the codebase with no indication that this happened. BL-010 (signatures-only) should eliminate the budget overrun, but the silent drop is still a latent bug for very large projects.
**What to build:** After collecting source files for TASK 0b, compute the total char count before adding each file. If a file would exceed the budget, log a warning (file path + char count + budget remaining) and skip it. Add a summary line at the top of the TASK 0b prompt listing any dropped files by name, so the LLM knows the strategy may be incomplete.
**Expected gain:** Silent failures become visible in the pipeline log. LLM is warned rather than given an incomplete view with no context.

---

## BL-018 — TASK 1 chunked path: running type manifest across chunks
**Status:** done
**Context:** In the chunked path, TASK 1 processes each chunk of test files independently. Chunk 3 doesn't know what types were extracted in chunks 1 and 2. The stage-1 merge deduplicates after the fact, but per-chunk extraction may describe the same type differently in each chunk, requiring the merge to reconcile inconsistent descriptions.
**What to build:** After each chunk is processed, extract the list of type names and trait names mentioned in its output. Pass this manifest to the next chunk as a "types already extracted" header: "The following types have already been extracted — do not re-describe them, only add new requirements." Accumulate across chunks.
**Expected gain:** No duplicate type descriptions entering the merge step. Final requirements document is leaner and consistent.

---

## BL-019 — TASK 4: pre-classify failure type before analysis prompt
**Status:** done
**Context:** TASK 4 receives raw test/build output and tries to reason about it. But compile errors, runtime assertion failures, panics, and timeouts require completely different analysis. A generic prompt that handles all four cases produces generic rules.
**What to build:** Before calling the LLM, classify the failure by scanning the output for known markers: `error[E` → compile failure, `thread '...' panicked` → runtime panic, `FAILED` with `assertion` → assertion failure, no output / timeout → timeout. Pass the classification as a structured header in the TASK 4 prompt: "Failure type: compile | runtime_panic | assertion | timeout". Add a separate focused instruction block per failure type.
**Expected gain:** TASK 4 produces targeted rules for the actual failure class instead of hedging across all four. Genesis grows more precisely.

---

## BL-020 — grep-ast for TASK 0c sibling file compression
**Status:** done
**Context:** Considered compressing sibling source files to signatures-only before including them in the TASK 0c prompt, to save context budget.
**Why not needed:** To write behavioral assertions, the model needs to understand what sibling functions actually do — not just their signatures. Compressing siblings loses the semantic information needed for correct test generation. The correct approach is to select fewer but fully-read siblings (BL-014), not to compress all siblings.

---

## BL-021 — SQLite pipeline knowledge store (foundation)
**Status:** done
**Context:** All inter-step communication today is large prose blobs (genesis.md, functional_requirements.md, coverage_strategy.md). Each task starts fresh without knowing what other tasks struggled with. No task can query "what compile errors recurred for this module in previous iterations" or "which types were hard to construct in tests" without parsing unstructured documents.
**What to build:** A `pipeline_knowledge.sqlite` database with four tables:
- `type_signals (project, file, type_name, difficulty, pattern, ts)` — written by TASK 0c, read by TASK 1
- `compile_patterns (project, module, error_category, example, iteration, ts)` — written by TASK 2 fix loops, read by TASK 2 on next iteration
- `genesis_rules (project, module_scope, category, rule_text, iteration, ts)` — written by TASK 4, read by TASK 1/2 filtered by scope
- `module_status (project, module, iteration, outcome, pass_count, fail_count, ts)` — written by TASK 3, read by TASK 4

Foundation for BL-022 through BL-025. No task reads from this store until its corresponding BL entry is implemented.
**Expected gain:** Enables structured inter-step knowledge flow without prose parsing.

---

## BL-022 — TASK 0c → TASK 1: type difficulty signals
**Status:** done
**Depends on:** BL-021
**Context:** When TASK 0c spends many attempts on a file failing to construct a particular type correctly, that is a signal that the type is complex or non-obvious. TASK 1 should extract unusually precise requirements for that type. Today that signal is invisible to TASK 1.
**What to build:** At end of TASK 0c per-file processing, write a row to `type_signals` for each type that appeared in a compile error across multiple attempts. TASK 1 reads all `type_signals` rows for the current project before building its prompt, and adds a section: "The following types were difficult to construct correctly in tests — extract especially precise requirements for them: [list]."
**Expected gain:** Requirements for hard-to-implement types become more specific, reducing TASK 2 compile failures for those types.

---

## BL-023 — TASK 2: inject concrete test assertions into generation prompt
**Status:** done
**Context:** TASK 2 generates source code from prose requirements. The test files ARE the ground truth specification — the actual `assert_eq!` / `assert!` calls are more precise than any prose summary of them. TASK 2 currently does not systematically see the assertions it must satisfy.
**What to build:** Before the TASK 2 generation call, scan all test files for the current module and extract just the assertion lines (`assert_eq!`, `assert!`, `assert_ne!`, `assert_matches!`) with 2 lines of surrounding context (the `let` bindings that set them up). Include these as a compact "assertion manifest" section in the TASK 2 prompt alongside the prose requirements.
**Expected gain:** TASK 2 has direct, unambiguous targets. Reduces iterations needed to satisfy assertion-level correctness.

---

## BL-024 — TASK 4: write structured rules to genesis_rules table
**Status:** done
**Depends on:** BL-021
**Context:** Currently TASK 4 appends prose to genesis.md. By iteration 6, genesis.md is a large document that TASK 1 must parse entirely to find rules relevant to the current module. Rules have no category, no module scope, and no priority — old rules and new rules are indistinguishable.
**What to build:** TASK 4 still updates genesis.md (for human readability), but also writes each extracted rule as a row to `genesis_rules` with: `module_scope` (which module the failure occurred in, or `*` for global), `category` (compile | trait_bound | module_declaration | type_construction | etc.), and `rule_text`. TASK 1 and TASK 2 query `genesis_rules` filtered by module scope + relevant categories instead of reading all of genesis.md.
**Expected gain:** TASK 1/2 receive only rules relevant to the current module and failure class. Genesis stops being a monolithic blob that must be parsed in full.

---

## BL-025 — TASK 2: read compile_patterns before generating
**Status:** done
**Depends on:** BL-021
**Context:** TASK 2 on iteration N+1 regenerates all files from scratch. It doesn't know that iteration N produced wrong module declarations in `lib.rs` or a missing trait bound in `error.rs`. It will make the same structural decisions again unless TASK 4 happened to encode those lessons in prose that TASK 1 extracted and forwarded.
**What to build:** TASK 2 fix loop writes a row to `compile_patterns` each time a compile error is resolved, recording: module, error category, what was wrong, what fixed it. Before the TASK 2 generation call, read all `compile_patterns` rows for the current module and include them as a "known pitfalls for this module" section in the prompt.
**Expected gain:** TASK 2 on each new iteration avoids the specific structural mistakes of previous iterations for that module. Reduces compile failure loops.

---

## BL-008 — Cargo.toml in TASK 0c prompt
**Status:** done (implemented as part of BL-003/BL-004/BL-007 whole-folder approach)
**Context:** The TASK 0c prompt never showed the LLM what dependencies were available in `[dependencies]`. This caused the LLM to generate `use serde::` when serde wasn't a dependency.
**What was built:** Cargo.toml content is now always-included (priority 1) in every TASK 0c per-file prompt.

---

## BL-026 — BUG: sys.exit(1) inside task0c_augment_module bypasses outer cluster loop
**Status:** open
**Severity:** high — kills the whole pipeline process instead of returning to the cluster loop.
**Location:** pipeline.py ~line 9816, inside `task0c_augment_module`.
**Root cause:** When every file hits `FILE_STALL_LIMIT` consecutive no-improvement attempts within a single pass, the function calls `sys.exit(1)`. This prevents the outer `ModuleAugmentState.hard_block` path from handling the situation: the cluster-level git commit is skipped, resume state is not written, and the pipeline exits with no recovery path.
**What to build:** Replace `sys.exit(1)` at line 9816 with a `return (coverage, covered_lines, False)` (or a new `_all_files_stalled=True` return element). The outer cluster loop already has hard_block handling — let it decide whether to accept the module and proceed.
**Expected gain:** All-files-stalled scenario handled gracefully: git commit written, sentinel written, outer loop can move to the next module instead of crashing.

---

## BL-027 — BUG: monitor wipe confirmation message never clears
**Status:** open
**Severity:** minor UX — yellow "↺ wiping and restarting with <url>" stays visible forever after a wipe.
**Location:** monitor.py `_run_action()` wipe branch (~line 998).
**Root cause:** `_action_msg` is set to the yellow wipe message when wipe is confirmed. Nothing ever clears it — not when wipe.sh finishes, not when the pipeline comes back up.
**What to build:** In the render loop, detect the transition from pipeline-not-running to pipeline-running (which happens after wipe.sh starts the pipeline). When that transition fires, clear `_action_msg` if it matches the wipe/graceful pattern. Alternatively: timestamp `_action_msg` and auto-expire after 60s.
**Expected gain:** The bottom message resets to the default state once the restarted pipeline is detected.

---

## BL-028 — BUG: monitor shows stale "all clusters covered" after early pipeline crash
**Status:** open
**Severity:** misleading UX — monitor displays completed state from previous run when pipeline crashes before modifying pipeline_state.md.
**Root cause:** `_parse_pipeline_state()` reads pipeline_state.md from disk. When the pipeline crashes on startup (e.g. missing dependency, vLLM not ready), no new state is written. The monitor reads the previous run's completed state and shows all clusters done and coverage numbers that reflect the old run.
**What to build:** In `_parse_pipeline_state()`, cross-check the "done" timestamps against the pipeline process start time (`_pipeline_start_time()`). If a phase was marked done BEFORE the current pipeline started, treat it as stale (show as `(stale)` rather than done). Already done for coverage JSON — same logic needed for pipeline_state.md parse.
**Expected gain:** Monitor accurately shows pipeline state is from a previous run when the current pipeline hasn't written any new state yet.

---

## BL-029 — CLEANUP: task0c_coverage_augmentation() is dead code
**Status:** open
**Severity:** low — confusion risk, maintenance overhead.
**Location:** pipeline.py ~line 9833, function `task0c_coverage_augmentation`.
**Root cause:** This was the pre-cluster TASK 0c implementation. The cluster-based loop in `main()` replaced it entirely. The function is never called. It still references the old tarpaulin-based coverage loop without cluster awareness.
**What to build:** Delete the function (lines ~9833–9906). Verify `grep task0c_coverage_augmentation pipeline.py` shows only the definition. Update tests if any reference it.
**Expected gain:** Removes ~75 lines of confusing dead code that could mislead anyone reading the pipeline.

---

## BL-030 — BUG: ModuleAugmentState accepts ceiling on a single at_ceiling pass
**Status:** open
**Severity:** medium — can lock a module at artificially low coverage when LLVM misreports all lines as covered for one pass.
**Location:** pipeline.py `ModuleAugmentState.status()` ~line 3415, and `_pass_had_work` logic in `task0c_augment_module` ~line 9800.
**Root cause:** `status()` returns `'ceiling'` the first time `last_at_ceiling=True`. `at_ceiling` is True when `_pass_had_work=False` (no LLM calls made because all files were filtered). One pass where all files are filtered by cfg/artifact is enough to permanently accept the module — even if the previous pass showed substantial uncovered lines.
**What to build:** Either:
- Track a consecutive `ceiling_passes` counter in `ModuleAugmentState`. Only return `'ceiling'` when `ceiling_passes >= 2` (two consecutive all-filtered passes).
- OR require `last_pct >= cluster_ceiling_estimate * 0.95` before accepting ceiling.
The second option requires passing `cluster_ceiling_pct` into `ModuleAugmentState` or into `status()`.
**Session 34 handover** noted this as a known caveat: "fake ceiling at low coverage" is not structurally prevented.
**Expected gain:** Single-pass LLVM noise doesn't permanently lock a module at low coverage.

---

## BL-031 — BUG: graceful stop _graceful_pending flag never auto-clears in monitor
**Status:** open
**Severity:** minor UX — graceful-pending banner stays visible after the pipeline actually consumed the sentinel.
**Location:** monitor.py, global `_graceful_pending` usage (~lines 781, 1026–1048).
**Root cause:** When the pipeline consumes `/tmp/pipeline_graceful_stop` (deletes the file), the monitor's `_graceful_pending` flag remains True. The user must press `g` again to cancel the display manually. The monitor has no polling to detect that the sentinel was consumed.
**What to build:** In the render loop (or in the status function), check: if `_graceful_pending` is True AND `/tmp/pipeline_graceful_stop` does NOT exist AND pipeline is running → auto-clear `_graceful_pending`. The pipeline deletes the sentinel the moment it picks it up, so this is a reliable signal.
**Expected gain:** Graceful banner disappears automatically once the pipeline has processed the stop request, without requiring user intervention.

---

## BL-032 — cluster_pre_check failure marked "done" — indistinguishable from success
**Status:** open
**Severity:** medium — misleading pipeline_state.md; 0.0% coverage modules silently proceed to strip.
**Location:** pipeline.py main cluster loop ~line 10742, `mark_cluster_augmented(..., 0.0)`.
**Root cause:** When `_cluster_pre_check` fails, all modules are marked augmented at 0.0% with no error status. `pipeline_state.md` shows `task0c/<cluster>/<module>` as "done" with `0.0%`. The monitor shows the cluster as complete. The subsequent strip phase sees a 0.0% sentinel and logs a WARNING but proceeds anyway. Reconstruction from 0% test coverage produces poor results.
**What to build:** Introduce a distinct "skip" or "error" status in `_mark_phase`. Use it when pre-check forces all modules to 0.0%. The monitor can display this distinctly (e.g. red "pre-check failed") instead of green "done". The strip phase can refuse to proceed if the sentinel contains a pre-check-error marker.
**Expected gain:** Operator sees immediately that a cluster was skipped due to toolchain failure and can intervene before reconstruction starts from near-zero coverage.

---

## BL-033 — all-files-stalled log should include per-file retrospective results
**Status:** open
**Severity:** minor — harder to diagnose why augmentation failed without retrospective summaries.
**Location:** pipeline.py ~line 9808–9815, the hard-block log inside `task0c_augment_module`.
**Root cause:** The log shows uncovered line counts and attempt history for each stalled file, but not the retrospective classification (CFG_GATED / UNREACHABLE / TESTABLE / NEEDS_INTERNALS) computed for each. If a file is stalled because every uncovered function was classified CFG_GATED, that's a success (expected ceiling). If classified TESTABLE, it's an actionable failure. Without this, the operator can't distinguish the two cases from the log.
**What to build:** In the hard-block log, append `file_retrospectives.get(rel_path, "(no retrospective)")` for each stalled file. `file_retrospectives` is already populated by `_task0c_retrospective` calls.
**Expected gain:** Hard-block log clearly shows whether each stalled file is a genuine ceiling (CFG_GATED/UNREACHABLE) or a prompt/design failure (TESTABLE).

---

## BL-009 — coverage_strategy.md injected into TASK 1 and TASK 2
**Status:** done
**Context:** coverage_strategy.md reveals import idioms and type construction patterns that help code generation too, not just TASK 0c. TASK 1 extracts requirements from tests — knowing the project's import conventions improves the specificity of what it extracts. TASK 2 generates code — knowing which types exist and how they're constructed reduces hallucination.
**What was built:** coverage_strategy passed to task1() and threaded into all TASK 1 synthesizer prompts; passed to task2() and injected into StaticFileListTask2Generator per-file prompts.

---
