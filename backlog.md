# Prompt-engineering backlog (May 2026)

Please refer to plan.md for the execution plan.

Open improvements to LLM prompts in pipeline.py, derived from a session-34 audit of
all 27 ask() call sites. Each item has a priority, a one-line description, the
affected sites, and a sketch of the change. "Done" entries stay listed (with their
commit hash) so future sessions can see what was already addressed.

## Priorities

P0 = mechanical, low-risk, ship now.
P1 = small behavior change, ship after smoke tests pass.
P2 = changes LLM behavior in a measurable way, validate on fixture + anyhow.

## Open

### P1 — Pass `_current_cluster.excluded_cfg` explicitly to gen prompt RULES section
- Site: main task0c gen prompt (~L9307 `_nightly_rules_for_cluster()`), medium-retry
  prompt (~L9374).
- Symptom: `_nightly_rules_for_cluster()` returns a prose sentence about nightly
  features but doesn't enumerate the inactive cfg predicates by name. The LLM must
  infer from prose whether `#[cfg(error_generic_member_access)]` is active or not. For
  clusters with multiple excluded predicates this guessing is error-prone.
- Fix: append an explicit machine-scannable block to both prompts:
  `"Inactive cfg predicates in this cluster: <comma list>"`. Distinct from the
  retrospective item (which targets classification) — this targets the generation
  prompt so the LLM avoids writing tests that rely on inactive predicates.
- Risk: low; purely additive context. `_describe_test_environment()` already shows
  active/excluded cfg — this makes it appear in the RULES section too.

### P1 — Surface per-file retrospective results in all-files-stalled log
- Site: `task0c_augment_module` hard-block log (~L9808).
- Symptom: when all files stall, the log shows uncovered line counts and attempt history
  but not the CFG_GATED / UNREACHABLE / TESTABLE classification from `_task0c_retrospective`.
  Operator cannot tell whether it's an expected ceiling (all CFG_GATED) or an actionable
  failure (TESTABLE functions not reached).
- Fix: append `file_retrospectives.get(rel_path, "(no retrospective)")` to the per-file
  line in the hard-block log. `file_retrospectives` is already populated during augmentation.
- Risk: low (log-only change). Also see BL-033 in BACKLOG.md.

### P0 — TASK 0b RULES section has hardcoded anyhow-specific examples
- Site: task0b_discovery prompt (~L4369-4371).
- Symptom: the RULES section of the prompt shows literal examples:
  `NEVER: use std::io or std::io::Error # not available...`
  `NEVER: error_generic_member_access|Request|provide()|crate::nightly`
  `NEVER: macro_export macros from internal mods  # e.g. anyhow!→...`
  These look like observations but are actually anyhow-specific facts baked in as prompt
  text. On any other project the model copies or generalises these as real observations,
  polluting strategy.md with wrong NEVER rules.
- Fix: move these three lines into a comment block prefixed `# EXAMPLE ONLY — replace:`.
  Or remove them entirely and trust the model to derive NEVER rules from the source.
- Risk: low; the examples are pedagogical, not functional — removing them loses nothing.

### P1 — TASK 0c INTERNAL MODULE RULES section is anyhow-specific
- Site: main gen prompt (~L9307-9337), medium-retry prompt (~L9547-9567).
- Symptom: the INTERNAL MODULE RULES section hardcodes:
  "This crate uses `#![no_std]` with `extern crate alloc`..."
  "Do NOT use `#[macro_export]` macros (e.g. `anyhow!`, `bail!`, `ensure!`)..."
  "anyhow's `Error` type is NOT `std::error::Error`..."
  "When implementing StdError::source() in a test helper, the field type MUST be
   `Box<dyn StdError>` (no Send+Sync)..."
  These appear for EVERY project, not just anyhow. On a std crate with no special error
  type, the `anyhow's Error` rule actively misleads the LLM. The no_std prelude rules
  are wrong for std projects.
- Fix: move these project-specific rules into the `RULES` section of coverage_strategy.md
  (TASK 0b output), which IS project-derived. The generation prompt should read rules from
  the strategy doc rather than embedding them as static strings. Remove anyhow-specific
  strings from the prompt body; keep only truly universal rules (zero-arg test fns,
  no PartialEq on non-PartialEq types, etc.).
- Risk: medium; requires TASK 0b to reliably produce RULES that cover what the hardcoded
  section covers. Validate on fixture + anyhow before removing the hardcoded block.

### P1 — _PRELUDE_ITEMS injection is no_std-specific but runs for all crates
- Site: ~L9437-9452, after block extraction in task0c_augment_module.
- Symptom: pipeline injects `use std::string::{String, ToString}`, `use std::vec::Vec`,
  `use std::boxed::Box` etc. into every generated test block regardless of whether the
  crate is `#![no_std]`. For std crates these are redundant (already in prelude) and
  cause "unused import" warnings; on stricter codebases they may be errors.
- Fix: read the module's `src/lib.rs` for `#![no_std]` before deciding whether to inject
  these prelude items. Only inject when `#![no_std]` is confirmed. The `extern crate alloc`
  line is the clearest signal.
- Risk: low; the injection only fires when the imports are absent, so in practice it
  won't double-inject on std crates — but it does add noise.

### P1 — _FILE_CONSTRAINTS dict is anyhow-specific and hardcoded in pipeline
- Site: ~L9180-9260, inside task0c_augment_module.
- Symptom: file-specific rules for error.rs, kind.rs, ptr.rs, fmt.rs, context.rs, lib.rs,
  wrapper.rs are compiled into the pipeline source. These rules are genuinely useful for
  anyhow but appear for any project with files of the same name (many Rust crates have
  an error.rs or lib.rs). The context.rs rules specifically mention `Context` (anyhow's
  trait) — on a different project Context may be a completely different type.
- Fix: store per-file constraint notes in coverage_strategy.md under a new FILE_NOTES section.
  TASK 0b already analyzes source — it can produce file-specific notes. Pipeline reads
  `FILE_NOTES` section from strategy and injects matching entry by filename. Fall back to
  the hardcoded table only if no TASK 0b note exists for that file.
- Risk: high; the hardcoded rules prevent a large class of anyhow-specific compile errors.
  Implement TASK 0b FILE_NOTES derivation first, validate it produces equivalent rules,
  then remove the hardcoded table. Not a one-step change.

### P1 — coverage_strategy.md never updated after TASK 4 learns type errors
- Site: entire pipeline flow.
- Symptom: TASK 0b writes coverage_strategy.md once at the start and it is read for the
  entire run. TASK 4 may discover that the TYPES or PITFALLS section in strategy.md is
  wrong (e.g. wrong construction expression for a type, a pitfall not listed). TASK 4
  updates genesis.md but NOT strategy.md. TASK 0c on every subsequent pass still reads
  the stale, wrong type hints. The error can recur for the entire run.
- Fix: after TASK 4 updates genesis.md, optionally run a lightweight strategy patch call:
  read the new genesis rules, check if any contradict strategy.md TYPES/PITFALLS lines,
  emit targeted line replacements. This is additive — only update lines that have direct
  evidence of being wrong. Do NOT regenerate the whole strategy (expensive and risky).
- Risk: medium; needs careful scoping to avoid the patch call producing contradictions.

### P2 — TASK 4 genesis hard-capped at 3,000 chars — middle content silently dropped
- Site: task4() ~L6656: `_t4_genesis_cap = 3_000`.
- Symptom: genesis is capped at 3k chars with head-weighted truncation (2k head / 1k
  tail). As genesis grows over iterations (adding rules per failure), the middle sections
  are invisible to TASK 4 when it decides what to update. TASK 4 updates genesis.md
  based on a truncated view — it may re-add rules that are already in the middle, or fail
  to notice conflicts with middle-section rules it can't see.
- Fix: raise to `min(len(genesis), SINGLE_CALL_CHAR_LIMIT // 8)` — roughly 8-10k chars at
  current context budget. This still fits easily in the TASK 4 prompt alongside test output
  and functional_req. Use 50/50 head/tail split since new rules are often in the MIDDLE of
  an evolving genesis, not just the tail.
- Risk: low; larger genesis context gives TASK 4 better self-awareness at modest token cost.

### P2 — TASK 2 has no visibility into previous iteration's generated code
- Site: main reconstruction loop in main(), TASK 2 call path.
- Symptom: TASK 2 regenerates all source files from scratch each iteration using only
  functional_requirements.md + genesis.md. It cannot build on iteration N's "mostly
  correct" code — it must re-derive everything. If iteration N had the right module
  structure but one wrong function implementation, iteration N+1 may get the module
  structure wrong again while fixing the function.
- Fix: after TASK 3 fails, write a condensed summary of what the previous iteration's
  code looked like (public function signatures + structural outline only, no bodies) to a
  temp file. Pass it to TASK 2 as an additional context section: "Previous iteration
  output (structure that COMPILED — use as a structural starting point, fix logic errors
  only)." This gives the model a scaffold to build on rather than starting cold.
- Risk: medium; may cause anchoring to previous mistakes. Only safe if the compile check
  passed (build_ok). Add a flag TASK2_SCAFFOLD_PREVIOUS in .env to toggle.

### P2 — old_block "do not repeat" instruction too far from the old_block content
- Site: main task0c gen prompt, ~L9297 vs ~L9295 (secs['old_block'] near end).
- Symptom: the instruction "Write ONLY new test functions... do NOT copy or repeat any
  test from them" appears near the TOP of the generation prompt (before all the source
  sections). The old_block content is rendered at the VERY END of the prompt. The LLM
  must hold the "don't repeat" rule across potentially 100K chars of context. On large
  prompts this attention gap increases repeat probability.
- Fix: add a one-line reminder immediately BEFORE secs['old_block'] in the prompt:
  `"↑ ALREADY ON DISK — do NOT copy any function from this block into your output:\n"`.
  Keep the main instruction at top too — this is a duplicate for emphasis, not a move.
- Risk: very low; purely additive.

### P2 — what_worked section shows patterns but not which lines they covered
- Site: _build_task0c_prompt_sections, what_worked_section (~L4507-4514).
- Symptom: what_worked says "covered N more lines, reuse these patterns" with condensed
  function signatures. The LLM cannot tell from a signature whether the pattern is
  relevant to the specific uncovered lines it's now targeting.
- Fix: store alongside the condensed signature which UNCOVERED lines it moved
  (e.g. "covered lines {45, 67, 89} — pattern: `let e = Error::msg(…)`"). Inject as a
  mapping: `line N: covered by pattern X`. The LLM can then prioritise patterns that
  covered lines structurally similar to the current targets.
- Risk: medium; requires storing line-to-pattern mapping in file_what_worked, not just
  signatures. The mapping is available at the point the coverage improvement is measured.

### P2 — TASK 1 prompt has no "derive only from tests" guardrail
- Site: _single_prompt (~L4843-4904), chunk prompts.
- Symptom: TASK 1 is asked to produce requirements from tests but no instruction
  explicitly forbids inventing requirements for behaviors not in the test assertions.
  With api_surface as structural ground truth, the model now knows the right shapes —
  but it may still invent behavioral contracts (return values, error conditions) that
  no test asserts. Requirements invented beyond what tests assert lead to TASK 2
  implementing untested code paths.
- Fix: add one sentence near the end of the requirements prompt: "Do not add a requirement
  for any behavior unless you can point to a specific assertion in the tests above that
  mandates it. Silence in the tests = no requirement for that behavior."
- Risk: low. Purely additive constraint. May slightly reduce requirement document length
  (which is a feature, not a bug — more precise requirements = fewer false TASK 2 targets).

### P1 — Pass `_current_cluster.excluded_cfg` to retrospective classifier
- Site: `_task0c_retrospective` (~L8104).
- Symptom: LLM has to guess whether `#[cfg(foo)]` is active or excluded. Misclassifies
  TESTABLE vs CFG_GATED for nightly-gated code.
- Fix: read `_current_cluster.excluded_cfg` (already module-level), pass into the
  retrospective as an explicit list ("These cfg predicates are NOT active in this
  cluster: ..."). Same idea for the gen prompt's RULES section.
- Risk: low; the LLM gets MORE truthful context.

### P1 — Show last sibling's test function names (not bodies) in task0c gen prompt
- Site: main task0c gen (~L9055), inner quick-retry (~L9312).
- Symptom: LLM occasionally regenerates already-covered cases because it can't
  cheaply see what's been tested before. `_old_block` shows the latest sibling but
  not earlier passes.
- Fix: read all sibling files via `_read_all_gen_tests`, run `_condense_rust_to_signatures`
  on them, inject as a short "Already tested:" section. Bounded to a small cap so
  it doesn't blow the budget on later passes.
- Risk: medium; changes generation shape. Validate via fixture's "with-sibling"
  smoke test before shipping.

### P2 — `with_retry` should feed the failure reason back to the LLM
- Sites: `LLMDependencyGraph._parse` (~L1882), `LLMWorkspaceScanner.scan`
  (~L1393), `discover_test_command` (~L2937), `task0_project_profile.attempt`
  (~L4047), `task0b_discovery.attempt` (~L4287), `task0_project_context.attempt`
  (~L4584), `StaticTask1Synthesizer._chunked.chunk_attempt` (~L4882),
  `StaticTask1Synthesizer._chunked.synthesis_attempt` (~L4954),
  `LLMTask1Synthesizer._chunked.chunk_attempt` (~L5257),
  `StaticTask2Generator.generate.attempt` (~L5612).
- Symptom: on format-violation, retries send the IDENTICAL prompt. Deterministic
  failures burn tokens forever.
- Fix: extend the `attempt()` callbacks to take an optional `last_response` and
  `last_violation` string, and have `with_retry` pass them on the next call.
  Each attempt can append a line like "Previous response did not contain the
  FILE marker for X; output it now and nothing else."
- Risk: low (purely additive); needs a small API change to `with_retry`.

### P2 — Show previous genesis.md diff in TASK 4 update prompt
- Sites: `task4` build prompt (~L6633) and test prompt (~L6790).
- Symptom: when an iteration regresses, the prompt says "previous iteration
  regressed by N tests" but doesn't show what genesis.md change caused it. LLM
  can oscillate, re-introducing the same bad rule.
- Fix: hash the prior genesis.md content; if a previous version exists in git
  (the workspace IS a git repo), generate a short summary of the last change
  ("Previously added: 'Rule X about Y'") and include it. Alternative: track a
  small `genesis_history.txt` outside the prompt context that the function
  reads.
- Risk: medium; depends on git workspace mode being active.

### P3 — Replace hard `test_files[:25]` cap with budget+complexity ranking
- Sites: `task0_project_profile` (~L4011), `task0_project_context` (~L4549).
- Symptom: on projects with >25 test files (anyhow has 34), the last ~25%
  silently get dropped. The 25 picked are in `sorted()` order — not by
  importance.
- Fix: use the same priority-by-complexity sort as `task0b_discovery` (~L4207)
  and fill until the per-file budget is exhausted. Drop the hard cap.
- Risk: low.

### P1 — Structured event log for pipeline observability
- Site: new helper `_emit(event, **fields)` writing `logs/events.jsonl`; wire into
  pipeline.start, cluster.{start,done}, task.{start,done}, file.{start,skipped,
  stalled,done}, llm.{request,response,error}, compile.{start,success,fail},
  test.{start,pass,fail}, coverage.measured, git.commit, guard.fired.
- Symptom: today, "is the pipeline alive or stuck" requires tailing
  `pipeline_anyhow.log` and `stream.log` and guessing from timestamps. There is
  no machine-readable record of what happened, when, and which determ commit
  produced it. Coverage measurements, LLM call durations, and stall events are
  not queryable.
- Fix: append-only JSONL with fixed schema `{ts, product, pipeline_sha, event,
  task, cluster, module, file, attempt, duration_ms, data}`. Content-address
  prompts/responses to `prompts/{sha256}.txt` and store
  `{sha256, char_count, input_tokens, output_tokens}` in the event so events.jsonl
  stays small (~90% of context is repeated across calls — strong dedup).
  Replace existing `_guard()` writes with `guard.fired` events for unification.
- Followups: tiny `monitor_events.py` reading the tail and flagging "no event in
  N minutes". Later: ingest events.jsonl → SQLite for ad-hoc queries.
- Risk: low. Strictly additive; no behavior change.

## Deferred

### project_profile + project_context merge — not worth the risk
~90 references touch these two artifacts. The two docs serve different
downstream prompts (profile → TASK 2 structural checks; context → TASK 0c
behavioural section). The only saving is one LLM call at pipeline startup,
which is cached on first run and amortises to ~0 over the rest of a run.
Risk-adjusted value is negative — skipped.

## Closed

### P0 — Idempotent gen_tests reattach (orphan-siblings bug, session 34, 85e9526)
Strip preserves *.gen_tests_N.rs siblings but reduces parent src/foo.rs to
just its path-mod decls. TASK 2 wipe+regenerate produced a fresh lib.rs
without those decls, and the reattach guard `_gen_tests_count > 0 → skip`
became a permanent no-op. Now per-pass idempotent via new helper
_has_gen_tests_decl. This bug had been silently zeroing every TASK 0c
coverage contribution since the sibling-file refactor.

### P1 — Drop stripped decl-only src from TASK 1 input (session 34, be1d185)
After strip, src/foo.rs is just `#[cfg(test)] #[path] mod gen_tests_N;`
lines. New helper _is_gen_tests_decl_only_src filters these out of
collect_test_files so they don't pollute TASK 1's prompt as fake test
content.

### P1 — Ingest examples/*.rs as TASK 1 usage signal (session 34, 87db703)
New collect_examples() walks <crate>/examples/, returns (rel, content)
pairs. main() appends them to t1_input with a clear inline header
explaining they are demos, not assertions. Also restricted
_capture_api_surface to <module>/src/ so example files aren't mistakenly
treated as part of the public API.

### P1 — TASK 4 api_surface awareness (session 34, 04c3ce0)
task4() now takes api_surface=, and when non-empty the prompt gains a
section explaining that structure was given as ground truth — methodology
updates should focus on behavioural extraction, signature drift is a
generation-discipline issue not a methodology gap.

### P1 — api_surface conformance check (session 34, eb78be8)
validate_task2_api_surface_conformance() extracts (kind, name) pairs for
pub fn/struct/enum/trait/type/mod from both the captured api_surface and
the regenerated source. Missing items become a drift report. Soft signal:
logged immediately, appended to test_output if TASK 3 fails so TASK 4
sees structural drift distinctly from cargo failures.

### P1 — Capture pre-strip API surface as TASK 1 ground truth (DONE — session 34)
- Sites: `_capture_api_surface` (new helper), `load_api_surface`,
  `_format_api_surface_section`, all four TASK 1 prompt builders
  (StaticTask1 _single/_chunk/_synthesize_group/_synthesis,
  LLMTask1 overrides, _task1_proc_macro).
- Symptom: TASK 1 had to derive BOTH structure (signatures, struct fields,
  enum variants) AND behaviour from tests alone. Tests exercise the API
  but don't enumerate it — public items not exercised by a test would
  silently disappear in the reconstruction, costing iterations to
  rediscover.
- Fix: just before strip, `_capture_api_surface` runs
  `_condense_rust_to_signatures` on every non-test .rs in the module and
  writes the concatenated skeleton to
  `.original_structure/<module>.api.md`. The dir survives strip (lives at
  project root, not under src/). TASK 1 calls `load_api_surface()`, threads
  the text into every prompt builder via a new `api_surface=""` parameter,
  and `_format_api_surface_section` wraps it with a "ground truth — do not
  contradict shapes shown below" header. Capped at ~25% of
  SINGLE_CALL_CHAR_LIMIT (2/3 head, 1/3 tail) so it can't crowd out tests.
- Note: changes the experimental claim from "reconstruct from tests alone"
  to "structure given, behaviour derived from tests". The new framing
  should be reflected in genesis.md the next time TASK 4 updates it.
- Verified: fixture's API surface is 960 chars after capture, contains
  every public fn signature with bodies elided, round-trips through
  load_api_surface, and wraps correctly via _format_api_surface_section.

### P0 — Replace `text[-cap:]` with `smart_truncate` everywhere (DONE — session 34)
- Sites: `_synthesize_group` (StaticTask1 + LLMTask1), `_task1_proc_macro`,
  `Task2 plan_prompt`, `Task2 file_prompt`, `Task2 LLM file gen`, `generate_fix_loop`,
  `task4` (`_tail_to_line` and `prev_truncated`).
- Symptom: tail-keep truncations lose the BEGINNING of long inputs.
  functional_requirements.md, genesis.md, requirements partials, and source
  blocks were truncated from the head, losing intros / type definitions.
- Fix: use `smart_truncate(text, total_limit=cap, head=cap//3, tail=2*cap//3)`
  where the intro matters, and `head=cap//4, tail=3*cap//4` for error/output
  blocks where the tail matters more.

### P0 — Cap `failure_log` in TASK 4 prompts (DONE — session 34)
- Site: TASK 4 build prompt (~L6633), test prompt (~L6790).
- Symptom: `load_failure_log` returns the full log; over many iterations it
  grows multi-KB and dominates the budget, shrinking every other section.
- Fix: cap via `smart_truncate` to ~20% of SINGLE_CALL_CHAR_LIMIT with a 1/4 + 3/4
  head/tail split — recent entries are most actionable; early entries provide
  baseline context.
