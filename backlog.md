# Prompt-engineering backlog (May 2026)

Open improvements to LLM prompts in pipeline.py, derived from a session-34 audit of
all 27 ask() call sites. Each item has a priority, a one-line description, the
affected sites, and a sketch of the change. "Done" entries stay listed (with their
commit hash) so future sessions can see what was already addressed.

## Priorities

P0 = mechanical, low-risk, ship now.
P1 = small behavior change, ship after smoke tests pass.
P2 = changes LLM behavior in a measurable way, validate on fixture + anyhow.

## Open

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
