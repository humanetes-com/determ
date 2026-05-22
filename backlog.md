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

## Closed

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
