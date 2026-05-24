# Prompt-quality and budget-discipline improvements

Source: systematic prompt audit (session 2026-05-24).

Scope: every LLM prompt in pipeline.py. Goals: (1) close functional bugs
where context is missing or misleading, (2) make budget accounting honest
across all tasks, (3) remove project-specific leakage so prompts work on
any Rust crate, (4) drop redundant/repeated text.

Out of scope (intentional): prompt section ordering (model attention is
broad enough that order is a weak signal compared to presence).


## Phase 0 — shared infrastructure

P0.1  Add prompt-size logging.
      Site: every `ask(prompt, ...)` call site, or wrap in helper.
      Cleanest: emit length inside `ask()` itself (pipeline.py uses an
      `ask()` wrapper that already logs/streams). Add an `_emit` event
      `prompt.size` with {chars, task} so monitor.py can chart it.
      Acceptance: every LLM call logs char count.

P0.2  Consolidate budget caps.
      Today two competing caps exist: SINGLE_CALL_CHAR_LIMIT (~318k) and
      TASK0C_PROMPT_CHAR_CAP (used in task0c fix loop). Decide one
      authoritative cap per task tier and document. Likely just standardize
      on the dynamically-computed SINGLE_CALL_CHAR_LIMIT for everything;
      task0c can still use its own override but referenced through one
      constant per task.
      Acceptance: grep "_CHAR_LIMIT\b" shows one constant per task family.

P0.3  Helper for honest scaffolding measurement.
      Add `_measure(prompt_template, **slots)` that returns
      `len(prompt_template.format(**{slot: "" for slot in slots}))`. Use
      this in fill-down sites instead of a magic "+3000" overhead estimate.
      Acceptance: every fill-down site replaces its constant with this call.


## Phase 1 — severe functional bugs (highest impact)

P1.1  (S1) Multi-file awareness in per-file task2 prompts.
      Sites: pipeline.py:6228 (LLMTask2Generator file_prompt),
             pipeline.py:6520 (StaticFileListTask2Generator).
      Add a new section to the per-file prompt:

        Crate `{module}` consists of {N} source files, each generated in a
        separate call. The complete file list is:
          [✓] src/lib.rs       (generated)
          [→] src/error.rs     (this call)
          [ ] src/chain.rs     (pending)
          ...

        For files marked [ ]: trust they will exist. Reference items via
        their declared `mod` paths.
        For lib.rs/main.rs: declare modules and re-exports; do NOT inline
        bodies that belong to other files.

      Pass the full file_paths list into the prompt builder; mark current
      with `→`, already-generated with `✓`, pending with empty.
      Acceptance: lib.rs prompt no longer makes the LLM think it must
      implement everything.

P1.2  (S2) Pass functional_requirements to task2 fix loop.
      Site: pipeline.py:6918 (generate_fix_loop prompt).
      Today: error + source + extra_context only.
      Fix: accept `functional_req` as an arg, add a "Spec:" section before
      "Current source files:". Cap with smart_truncate against remaining
      budget after error + source.
      Wire site: pipeline.py task2_inner_loop calls generate_fix_loop —
      pass functional_req_path through.
      Acceptance: fix-loop LLM sees the spec and won't delete required
      calls just because they don't yet have an implementation.

P1.3  (S3) Disambiguate test files in task2 plan/per-file prompts.
      Sites: pipeline.py:6157 (plan_prompt), 6228, 6520.
      Today: prompts say "Do NOT include test files" at the end while
      simultaneously embedding test files as context.
      Fix: when test files appear, wrap them with a clear header:

        --- READ-ONLY TEST FILES (do NOT regenerate — these are the spec) ---
        <TESTFILE path="...">...</TESTFILE>
        --- END READ-ONLY ---

      Drop the "Do NOT include test files" line — the section header
      already says it. Same fix for existing_test_files in prev section.
      Acceptance: no contradiction between context and instruction.

P1.4  (S4) Static task1 should also exclude test helpers.
      Site: pipeline.py:5074 (StaticTask1Synthesizer._single_prompt).
      The LLM variant at 5479 already says "Do NOT include `fn test_*`...";
      port the same rule to the Static variant.
      Acceptance: both task1 backends explicitly exclude test-only symbols.


## Phase 2 — budget honesty (silent overflow risk)

P2.1  (M1) Cap genesis in monolithic task2.
      Site: pipeline.py:6022.
      Today: `genesis` inserted unconditionally with no cap.
      Fix: `trim_to_line(genesis, _GENESIS_CAP, ...)` like other tasks.

P2.2  (M2) Cap whole_project_context in monolithic task2.
      Site: pipeline.py:6029.
      Fix: smart_truncate against remaining budget, share the budget with
      genesis and functional_req as in the per-file generator.

P2.3  (M3) Reorder FR/API/tests so FR survives truncation.
      Site: pipeline.py:6520 (StaticFileListTask2Generator).
      Today: api_section measured into primary_size before FR, and FR
      itself has its own cap. If FR is large it can be truncated while
      secondary API still gets full budget.
      Fix: in primary_size accounting, treat FR as a HARD floor — measure
      it first uncapped (only smart_truncate against the whole budget if
      it alone overflows). Subtract from budget. Then everything else
      shares the remainder.

P2.4  (M4) Add budget reservation to task1 single-prompt.
      Site: pipeline.py:5074.
      Today: caller passes tests_section with no per-prompt enforcement.
      Fix: build tests_section inside the prompt builder so the fill-down
      pattern (fixed-overhead + remainder for tests) is applied
      consistently.

P2.5  (M5) Unify task0c fix prompt cap.
      Site: pipeline.py:10060.
      Replace TASK0C_PROMPT_CHAR_CAP with a single derived cap (probably
      SINGLE_CALL_CHAR_LIMIT_TASK0C from line 543, which already exists).

P2.6  (M6) Audit each section in task0c main prompt for caps.
      Site: pipeline.py:9820.
      Inspect `secs[...]` builder to confirm every section (strategy,
      history, retrospective, context, full_source, fn_bodies, symbols,
      examples, integ_tests, siblings, old_block) has a cap or is small
      by construction. Add caps where missing.

P2.7  (M7) Audit task4 prompts for uncapped sections.
      Site: pipeline.py:7273, 7315.
      Confirm truncated_output, source_section, failure_log_section all
      respect a budget; add fill-down where missing.


## Phase 3 — portability (remove anyhow/argh-specific leakage)

P3.1  (R3) De-hardcode rust_module_rules from LLMTask2Generator.
      Site: pipeline.py:6093-6105.
      Names like EarlyExit, CommandInfoWithArgs, FlagInfo, Optionality,
      SubCommandInfo are argh-specific. They leak from when the pipeline
      was tested on argh.
      Fix: derive the "root types" list from api_surface (types declared
      in lib.rs's section). Keep the general rule "Submodules reference
      root types as `use crate::TypeName;`" but populate the example with
      whatever the current project actually exposes.

P3.2  (R4) De-hardcode INTERNAL MODULE RULES in task0c.
      Sites: pipeline.py:9840-9846, 10076-10082.
      `StdError`, `String`, `Vec`, `Box`, `format!`, `fmt` come from
      anyhow's prelude. On a no_std crate, this list is wrong.
      Fix: read the prelude actually injected by the test-block scaffolding
      (the `use` lines that the template wraps around gen_tests) and
      substitute that list at prompt build time. The prelude is already
      computed for each module — pass it into the prompt builder.

P3.3  (R5) Drop speculative module names in task2 monolithic prompt.
      Site: pipeline.py:5982.
      The text "If a submodule file (errors.rs, helpers.rs) is generated"
      uses made-up names. Replace with "If any submodule file foo.rs is
      generated, declare it as `mod foo;` in lib.rs."

P3.4  Confirm no other prompts hardcode project specifics.
      grep -n '\b\(anyhow\|argh\|EarlyExit\|StdError\)\b' pipeline.py
      and review every hit. Remove or generalize.


## Phase 4 — polish (low risk, removes noise)

P4.1  (R1) Stop emitting FILE_FORMAT_INSTRUCTION twice.
      Sites: pipeline.py:5126+5127, 5425+5426.
      Keep one instance per prompt — preferably at the top after the role
      statement, since that's where instruction is most attended.

P4.2  (R2) Stop emitting "Output ONLY ..." twice.
      Site: pipeline.py:6520 (lines 6522 and 6530 both say it).

P4.3  (R6) Move task4 delim_instruction to AFTER the analysis steps.
      Site: pipeline.py:7273, 7315.
      The model should focus on analysis first, formatting last.


## Phase 5 — earlier task2-context audit follow-ups

These were identified in an earlier prompt audit (same session). Implement
together with Phase 1/2.

P5.1  Cap prev_files_section in StaticFileListTask2Generator.
      Site: pipeline.py:6520.
      Today: uncapped — file 10 of 10 can carry 100+ kB of prior content.
      Fix: include only the most recent N (e.g. 3) prior files in full;
      for older files include only API signatures (use _split_api_surface_by_file
      to derive). Or hard-cap and condense.

P5.2  Loosen primary test matching from exact stem to prefix/substring.
      Site: pipeline.py:6442.
      Today: `_rel_stem == _file_stem` exact match. For src/error.rs,
      tests/test_chain.rs is rejected even though it tests error.rs items.
      Fix: match if file_stem appears in _rel_stem or vice versa (after
      stripping a "test_" prefix from _rel_stem). Keep substring guard
      tight enough to avoid false matches.

P5.3  Sort secondary tests by relevance to current file.
      Site: pipeline.py:6502.
      Today: alphabetical.
      Fix: rank by (a) stem similarity to current file, (b) number of
      times the file's API surface names appear in the test source.

P5.4  Honest scaffolding overhead.
      Site: pipeline.py:6484.
      Today: hardcoded +3_000.
      Fix: use P0.3 helper.

P5.5  Detect duplication between whole_project_context and primary/secondary
      tests. If whole_project_context tests are a superset of primary +
      secondary, drop whole_project_context.
      Site: pipeline.py:6470 — and where _format_whole_project_context is
      assembled (line ~6585).


## Implementation order

1. Phase 0 (P0.1-P0.3): infrastructure first so we can measure improvements.
2. Phase 3 (P3.x): de-hardcode before changing semantics — easier to test
   regressions when prompts aren't carrying project-specific noise.
3. Phase 1 (P1.x): functional bugs.
4. Phase 2 (P2.x): budget honesty.
5. Phase 5 (P5.x): task2 context refinements.
6. Phase 4 (P4.x): polish.

After each phase: bash check.sh (catch breakage) and a short pipeline
smoke (TASK 2 on anyhow stable cluster), verifying prompt sizes don't
regress beyond budget.


## Verification

- bash check.sh after every Python edit (CLAUDE.md mandate).
- After P0.1 lands, watch prompt sizes in logs/events.jsonl during a
  fresh anyhow run; flag any > 90% of SINGLE_CALL_CHAR_LIMIT.
- After P1.1: confirm lib.rs prompt for anyhow no longer says "single
  file is hard" in its thinking output (logs/stream.log).
- After Phase 3: run pipeline against a non-anyhow, non-argh fixture if
  available; or visually verify no project-specific names remain in
  generic prompts.
