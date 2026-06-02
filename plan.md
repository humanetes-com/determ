# Pipeline plans

Four plans live here. Plan 4 (this section, NEW) is the next architectural
move — TDD-scoped per-fn driver. Plan 3 = fixes derived from the
2026-05-26 morning retrospective. Plan 2 = skeleton-based reconstruction
baseline. Plan 1 = prompt-quality audit, complete, historical.

---

# Plan 5 — task0c-final: consolidate gen_tests before strip (BACKLOG, deferred)

Scheduled AFTER the CODE_ARCHITECTURE.md migration slices land. Do not start
until slices 4b–7 are done.

Problem
- Each task0c augmentation pass for `src/{stem}.rs` emits a sibling
  `{stem}.gen_tests_{N}.rs` + a path-mod decl `#[cfg(test)] #[path=...] mod
  gen_tests_{N};`. Many passes ⇒ N siblings + N decls per file. Sprawl, and any
  future single-module collapse would collide same-named `fn test_*`/helpers/`use`.

Step (new task0c-final, runs after the last pass, before bake+strip)
- Per source file, fold its `gen_tests_{N}` siblings into ONE
  `{stem}.gen_tests.rs`; replace the N path-mod decls with one.
- Namespace each pass as an inline `mod pass_{N} { use super::*; <pass body> }`
  ⇒ identical fn/helper names across passes never collide (no renames needed).
- Per-file only — gen_tests bind to their module via `use super::*`; a global
  merge would break `super::` paths. No cross-file merge.

Constraints
- Deterministic / AST-based via the syn extractor — NO regex (Rule 9). Add a
  `--merge-gen-tests` (or reuse merge-clusters machinery) subcommand; Python
  drives it.
- gen_tests are TEST code, not source bodies ⇒ reading/merging them pre-strip is
  allowed (Hard Rule 11 unaffected). Output must round-trip the existing
  `test_strip_preserves_gen_tests_verbatim` invariant on the consolidated file.
- Idempotent: re-running on already-consolidated state is a no-op.

Open: none material — per-file granularity + inline-`mod pass_N` namespacing is
the only coherent design (confirmed against the `use super::*` binding).

---

# Plan 4 — TDD-scoped per-fn driver (2026-05-27)

## Problem

The current Phase C driver walks `unimplemented!()` stubs file-by-file
and fills every one of them. This violates the spirit of tests-only
reconstruction in two ways:

1. **Unreachable fns get filled.** Private helpers that no test
   exercises still consume LLM calls. Their notes pollute the
   `<MODEL_NOTES>` section for unrelated fills. The resulting body is
   never validated by any test — only by `cargo check`, which only
   verifies it type-checks, not that it implements anything.
2. **Fns see one test, not all callers.** `_find_tests_for_fn` returns
   up to 4 tests that reference the fn name. The model gets a narrow
   view of how the fn is exercised. Bodies tend to be small (median
   38 chars on first-try) — minimal-passing rather than fully-correct.

The runtime-validation half of the architecture (end-of-pass cargo
test) is supposed to catch these. It does so coarsely — per pass, not
per fn — and the classifier's mapping back to fns is fragile.

## Approach

Keep the per-fn fill mechanics (prompt, fix loop, splice, parallelism
of 4). Change only the driver:

  - **What to fill** = fns reachable from at least one failing test
  - **What context to attach** = ALL tests that exercise this fn (capped
    by budget), not just `_find_tests_for_fn`'s top-4 picks
  - **When to stop** = when cargo test passes OR no progress across N
    iterations

Practically: after Phase A loads the cluster's skeleton, run
`cargo test` once. Every unimplemented body panics with
`not yet implemented` — the panic stack identifies the offending fn.
Parse that into a **frontier set** of fns to fill. Fill them in parallel
(reusing `_task2_fill_fns_one_by_one`'s machinery with a different stub
discovery). Re-run cargo test. Compute the new frontier (might include
fns that compile-passed but mis-implement, surfaced as runtime
assertion failures rather than `unimplemented!()` panics). Repeat.

Fns never reached by any test stay `unimplemented!()` forever —
correct under Hard Rule 11 (CLAUDE.md) and FR-022.

## Step-by-step

P4.1 — Frontier extraction
  - New `_extract_unimplemented_panic_frontier(test_output, source_dir)`
    parses cargo test stderr / panic output. Each `not yet implemented`
    panic has a `panicked at src/foo.rs:N` line. Map (file, line) →
    (file, fn_name, container) via the syn extractor's per-file fn
    enumeration. Returns `dict[(file, fn_name), set[test_name]]`.
  - On the first call (skeleton untouched), this yields every fn
    transitively reachable from any test. Subsequent calls return only
    the still-unimplemented frontier.

P4.2 — Test-driven stub discovery
  - Replace `_scan_unimplemented_for_cluster(target_src_dir, cluster)`
    with `_frontier_stubs_for_cluster(target_src_dir, cluster,
    frontier)`. Same return shape (list of fn records), filtered to
    members of `frontier`. Preserves leaf-first ordering within the
    frontier.
  - `_task2_fill_fns_one_by_one` gets a new param `frontier:
    dict[(file, fn_name), set[test_name]] | None`. When `None`,
    falls back to current behaviour (every stub). When set, only stubs
    in the frontier are processed.

P4.3 — All-caller tests in per-fn prompt
  - `_find_tests_for_fn` currently returns up to 4 tests via heuristic
    ranking. Augment with the EXACT test names from the frontier map:
    `frontier[(file, fn_name)]` is the authoritative set of tests
    that hit this fn at runtime. Include their full bodies (or
    truncated form if over budget) in the per-fn prompt's
    `<TESTS_EXERCISING_THIS_FN>` section.
  - Keep `_find_tests_for_fn`'s heuristic results as supplementary
    when the frontier set is small (e.g. < 2 tests).

P4.4 — Outer TDD loop in `_generate_one_cluster`
  - After Phase A (skeleton loaded + cargo check passing) and Phase B
    (test grouping skipped in per-fn mode):
    ```
    frontier = first run of cargo test → extract panic frontier
    iter = 0
    while frontier and iter < TDD_MAX_ITERS (default 5):
        _task2_fill_fns_one_by_one(... frontier=frontier ...)
        new_frontier = re-run cargo test → extract panic frontier
        if new_frontier == frontier (no progress): break
        frontier = new_frontier
        iter += 1
    ```
  - When the loop exits with frontier non-empty, those fns are
    genuinely intractable — accept their `// SKELETON_STUB_FAILED_ATTEMPT`
    annotation.

P4.5 — Test-exit recognition
  - cargo test return codes:
      0 = all tests pass → frontier empty → done
      101 with `not yet implemented` panics → fill them
      101 with runtime assertion failures → fill the implicated fn (the
          one whose body returned the wrong value)
  - Distinguish via panic message text. Assertion failures are harder
    to attribute — for now, treat assertion-failed tests as a
    secondary frontier: take the fn at the TOP of the test's call
    stack (the one the test calls directly), put it in the frontier
    for re-fill.

P4.6 — Wiring
  - Env `PHASE_C_DRIVER` defaults to `fn_walk` (current). Set to `tdd`
    to enable Plan 4. Keep both backends alive during validation so
    we can A/B compare on the same workspace.
  - Phase D (residual stub cleanup) gated: skip when driver=tdd —
    by definition the TDD loop already filled everything reachable
    from tests; residual stubs are correctly left unimplemented.

P4.7 — Metrics
  - New event `phase_c.tdd.iter` per TDD-loop iteration:
    `{cluster, iter, frontier_size, fills_done, fills_failed,
     test_pass_count, test_fail_count}`.
  - End-of-cluster summary includes "unreachable stubs" count — fns
    that no test touched, intentionally left `unimplemented!()`.

## Trade-offs vs current

  PRO  Faithful to tests-only reconstruction principle
  PRO  Each fn fill sees ALL its test callers (not 4)
  PRO  No wasted LLM calls on unreachable fns
  PRO  Per-iteration cargo test gives precise pass/fail signal
  PRO  Notes accumulate only for fns the model actually had to fill,
       not for fns that no test would ever validate
  CON  Cargo test runs more often (once per outer iter, vs once per
       pass). Mitigated by parallelism on the inner per-fn loop.
  CON  Runtime assertion failures need a heuristic to map back to
       fns. Crude but workable; classifier already exists for this.
  CON  Long-tail fns that depend on each other might need multiple
       outer iters to all converge. TDD_MAX_ITERS bounds this.

## Implementation cost

  P4.1   ~60 lines (panic-output parser, fn lookup)
  P4.2   ~20 lines (frontier filter)
  P4.3   ~30 lines (prompt section, budget handling)
  P4.4   ~40 lines (outer loop wiring)
  P4.5   ~20 lines (assertion-failure heuristic)
  P4.6   ~10 lines (env flag + Phase D gate)
  P4.7   ~15 lines (events)
  Total: ~200 lines of new code, mostly localised to
  `_task2_fill_fns_one_by_one` and a new helper module.
  Existing fn_walk driver kept intact behind the env flag.

## Validation

  - Smoke: fixtures/tiny cargo phase with PHASE_C_DRIVER=tdd should
    fill the 3 stub fns the test exercises and leave 0 unreachable
    (tiny has no unreachable fns).
  - Real run on anyhow: A/B against the current run's outputs.
    Metrics to compare: fns_filled, fns_unreachable, llm_calls,
    test_pass_count after cluster-done, wall-clock per cluster.

## When to abandon Plan 4

  - If frontier-extraction false-positives rate is high (assertion
    failures map to wrong fn) — would manifest as fills oscillating
    between fn A and fn B without resolving.
  - If TDD_MAX_ITERS is consistently hit before tests pass — outer
    loop isn't converging, signal that the prompt or notes don't have
    enough context.

## Resolved design decisions (2026-05-27 discussion)

### D1 — Parallelism: parallel across distinct fns, serial within a fn

The 4 worker threads stay. Per-fn lock (`{fn_key: Lock}` map) ensures
no two threads splice/fix the same fn concurrently. Two failures on
the same fn ALWAYS serialize — one fill may satisfy both, and a
concurrent re-fill would fight the first. Existing
`_task2_fill_fns_one_by_one` already does this for splice + cargo
check via `_splice_check_lock`. The TDD driver inherits it.

### D2 — Frontier ordering: shallowest panic first, then most-callers

Sort frontier entries by:
  1. ascending panic stack depth (shallowest = closest to a stub =
     fewer dependencies = easier to fix)
  2. tie-break descending by `len(caller_tests)` (more tests hit it
     → fixing it cascades to satisfy more failures per LLM call)
  3. tie-break ascending by (file, fn_name) for reproducibility

### D3 — Exhausted-fn re-entry only on changed context

When a fn exhausted in iter N reappears in iter N+1's frontier, the
TDD driver checks two conditions before retrying:
  - Did any OTHER fill land between iter N and N+1? (`fills_since`
    counter on the cluster state) — gives the model a different
    surrounding context.
  - Did new model-notes get added relevant to this fn? (check
    `_filter_model_notes_for_prompt` returns more entries than last
    attempt)
If either is true, retry. Otherwise skip — mark the fn as
permanently exhausted for this cluster. The state survives across
TDD iters via a `permanently_exhausted: set[(file, fn)]` on the
per-cluster work state.

### D4 — Cargo build failure: route into the compile-error frontier

cargo test wraps cargo build. If the build fails (rc=101 with
"error[E0xxx]" in stderr, no test panics), the TDD driver builds a
DIFFERENT frontier: every (file, fn) named in a compile error.
Falls back to the existing per-fn compile-fix loop. Once the cluster
builds again, resume the runtime-failure TDD loop.

### D5 — Per-test invocation timeout: 120s wrap, suspect last-filled

Single hung test blocks the whole test binary. Wrap each cargo test
invocation with shell `timeout 120s`. On timeout:
  - Mark all fns filled since the last successful cargo test as
    "suspect" — sorted by most-recent-first
  - Revert the most-recent suspect's body to `unimplemented!()` with
    `// SKELETON_STUB_HUNG_TEST` annotation
  - Re-run cargo test. If it completes (with the now-stub fn
    panicking), the revert was correct.
  - If it still times out, revert the next suspect. Cap at 3 reverts
    per timeout event.
Default 120s configurable via env `PHASE_C_TEST_TIMEOUT`.

### D6 — Unified localised loop abstraction: `_localized_test_loop`

Extract the common pattern shared by per-fn compile-fix, per-pass
test-fix, TDD per-failure inner loop, and TASK 3 inline retry into
one helper:

```python
def _localized_test_loop(
    *, target_fn: tuple[str, str],          # (file_rel, fn_name)
    failing_test: str | None,               # test name, or None for compile
    test_cmd: list[str],                    # how to verify
    cwd: Path,
    cluster_env: dict,
    prompt_builder: Callable,               # _build_per_fn_prompt or fix variant
    max_iters: int,
    max_stalls: int,
    ...
) -> {"converged": bool, "iters": int, "exhausted": bool}:
    """Focus one fn until a specific test passes (or compile succeeds).

    On each iter: fill fn → run test_cmd → if pass, done.
    Otherwise build fix prompt with the cargo output as error_excerpt,
    floor-stall on error count, splice retry, repeat.
    """
```

Then refactor existing call sites to use it:
  - `_task2_fill_fns_one_by_one`'s compile-fix loop → call with
    `failing_test=None`, `test_cmd=cargo check`
  - End-of-pass test-fix retry → call with the specific failing test
    name and `test_cmd=cargo test --test <stem>`
  - TASK 3 inline retry → same shape
  - New TDD per-failure loop → same shape

This is the "generic rule" interpretation: ONE function, called from
many places with different parameters. Same fix-loop machinery,
same floor-stall, same notes, same prompt building. Drives consistent
behaviour across the pipeline.

### D7 — Initial cargo test on skeleton

Untouched skeleton: every test panics on its FIRST `unimplemented!()`.
The iter-0 frontier is the set of fns called as the first
`unimplemented!()` reached by each test. This is naturally shallowest
first — leaf-first ordering emerges from test execution order without
the leaf-order metadata. The `.skeleton/{cluster}/_leaf_order.json`
artefact is still used as the secondary sort key when iter > 0 (when
the model has implemented enough that multiple fns are at the same
panic depth).

## Cluster scoping (unchanged)

Each cluster's TDD outer loop runs cargo test on its own filtered
test set (`_filter_tests_for_cluster`). Cross-cluster test failures
surface at TASK 3 (full cargo test over merged source).

## Revised step list

  P4.1 — Frontier extraction (panic + assertion + compile-error parsers)
  P4.2 — Test-driven stub discovery (filter by frontier)
  P4.3 — All-caller tests in per-fn prompt
  P4.4 — Outer TDD loop with D2 ordering + D3 re-entry + D4 compile fallback + D5 timeout
  P4.5 — Assertion-failure attribution heuristic
  P4.6 — `PHASE_C_DRIVER=tdd` env flag + Phase D gate
  P4.7 — Metrics + events
  P4.8 — Extract `_localized_test_loop` helper (D6); refactor existing
         test-fix sites to use it
  P4.9 — Per-fn lock map for D1 enforcement (defensive — current
         queue model already prevents same-fn concurrency, but the
         TDD frontier could theoretically queue the same fn twice
         from two different failures)

Implementation cost: bumped from ~200 to ~300 lines because of D6's
refactor and D5's timeout-recovery logic.

---

# Retrospective — 2026-05-26 (overnight run, session 40)

Pipeline ran ~13 hours on anyhow with the per-fn fix-loop + notes
architecture from session 40. Three clusters processed, 17 self-correction
notes accumulated, 2 clusters Phase-C-done. Below is what worked, what
didn't, and the resulting fix plan.

## Pipeline — logic and loops

### Wins
- Per-fn architecture: 22 first-try + 11 recovered = 75% net success rate
  (excluding the 4 protocol skips).
- Multi-pass retry firing as designed. `object_drop_front` got 3 chances
  across pass 1/2/3 in cluster `stable`, each with richer file context.
- Annotated stubs preserving failure history across passes (sentinel
  `// SKELETON_STUB_FAILED_ATTEMPT` correctly recognised by
  `_scan_unimplemented_functions`).
- Wipe-bug fix held — `.skeleton/` and `.skeleton-filled/` caches
  survived restarts; pipeline picked up from where it left off without
  re-extracting.
- Leaf-first ordering visible in logs: `vtable` filled first in every
  cluster (helper that other fns reference).
- Hard fns DID recover via fix-loop: `object_downcast` in stable
  converged on attempt 7; in anyhow_no_core_error on attempt 4.
- Notes accumulating with high quality, written in the model's own
  first-person voice, semantically usable on later attempts.

### Misses
1. **Test phase silently broken all night.** 3 `phase_c.pass.test` events,
   all rc=101 in 0.1s. Cargo crashed at command-parse time because
   `_find_tests_for_fn` returned `src/*.gen_tests_*.rs` siblings as
   `--test {stem}` targets. Those aren't standalone test binaries — they're
   imported via `#[path]` from src files. Whole runtime-validation half of
   the architecture sat idle.
   STATUS: fixed in pipeline.py (filter `test_paths` to those with `tests`
   in `Path.parts`). Takes effect on next restart.

2. **Nightly cluster's toolchain not applying.** Failed prompts for the
   nightly cluster show `error[E0554]`: `#![feature]` may not be used on the
   stable release channel — but we're explicitly in the nightly cluster.
   Either `cluster.toolchain` is None when it should be "nightly", or the
   `cargo +nightly` prefix isn't reaching the actual invocation. To diagnose.

3. **object_drop_front: 5 exhaustions across all clusters, never recovered.**
   The notes system extracted the lesson five times (4 of the 17 notes are
   TypeId/'static variants). The model knows the lesson. But the fix would
   require modifying the fn signature to add `T: 'static`, and the per-fn
   prompt forbids changing the signature ("Signature (verbatim, do not
   change)"). Architectural mismatch: note advises a fix the prompt rules
   prohibit.

4. **Multi-pass duplicate work.** 48 fills across all runs for a crate with
   ~30 unique fns. The `_scan_unimplemented_functions` correctly skips
   already-filled (non-stub) bodies, but each pass re-scans and re-iterates
   every stub including annotated ones — useful for genuine retry, but
   leads to wasted re-attempts when an annotated stub fails the same way.
   The fn's own past failure note doesn't strongly enough deter the model
   from repeating the approach.

5. **Body sizes small.** Median body 64 chars, 23/44 bodies under 50 chars.
   Many "successes" may be trivial fills that compile but don't implement
   real behavior. The compile loop validated they compile; tests would have
   revealed if they work — and tests never ran (miss #1).

## Prompts — context quality

### Wins
- Prompt size manageable (26-33 KB for fix prompts).
- MODEL_NOTES section reaches the model and grows across the run.
- `help:` lines from rustc are now visible (yesterday's
  --message-format-default change paying off).
- Annotated stubs in file context: the model sees prior failed attempts
  inline as comments while writing the new attempt.
- MACROS section visible (ensure!, bail!, anyhow! reachable to every fn).
- Tests filtered by file-stem + macro-aware: relevant tests reach the
  prompt even when the fn name doesn't appear in test text.

### Misses
1. **Notes deduplication is hash-based only — semantic duplicates slip
   through.** 4 of the 17 notes paraphrase the same TypeId/'static rule.
   Each costs prompt budget; the model sees redundant guidance.

2. **Note relevance filter not aggressive enough.** A fix-prompt for
   `object_drop_front` includes 8 notes. Most don't apply. Tier 1 (file/fn
   name match) should bubble TypeId notes higher than wrapper-struct notes
   when filling a TypeId-related fn — but the current filter ranks by
   simple substring presence.

3. **No `rustc --explain Exxxx` augmentation for stuck fns.** We have
   `help:` lines now, but for fns stuck 10+ attempts on the same error code
   the canonical explain output would be a stronger signal than re-reading
   the same help line. Identified yesterday as Option 2; deferred; now
   warranted by observed convergence problems.

4. **File context includes verbose attributes.** Top-of-file
   `#![allow(unused, dead_code, ...)]` and similar attrs are visible to the
   model unchanged. They eat tokens, don't help fill bodies.

5. **Cargo error excerpt may be too narrow.** `_focus_cargo_errors` keeps
   only errors whose `-->` mentions the fn's file. But a wrong fill in
   error.rs can break compilation in lib.rs (consumer of error.rs). Those
   lib.rs errors are filtered out, hiding the real symptom.

## Recurring confusion classes — rules to add to prompts

From the 17 extracted notes, six confusion classes recur:

1. `TypeId::of::<T>()` requires `T: 'static`. If T is generic without that
   bound and the signature is locked, you cannot use TypeId::of::<T>.
2. `&dyn Trait + Bounds` needs parentheses → `&(dyn Trait + Bounds)`.
3. Trait objects (&dyn Debug, *const dyn StdError) are unsized; cannot be
   stored by value. Format with `{:?}`, not `{}`.
4. Wrapper struct method-vs-field: when `wrapper.method()` fails, try
   `wrapper.field.method()`.
5. Generic parameters cannot have fields accessed. `(*this).field` where
   `this: *const E` is rejected; cast `*const E` to a concrete known struct
   first.
6. `NonNull<T>` doesn't auto-deref to `&T`. Use
   `unsafe { ptr.as_ref() }` to get a reference.

These are Rust-general (not anyhow-specific), should be in the base prompt
template, not relying on the notes system to re-discover them per-run.

---

# Plan 3 — fixes from 2026-05-26 retrospective

Numbered fixes ranked by expected impact. Each one is independently
implementable and testable. Notation: ⚑ = ready, ⏳ = depends.

## P3.1 ⚑ Constraint relaxation — let the model alter signatures when necessary

**Problem.** `object_drop_front` and similar stuck fns need a signature
change (add `T: 'static`) that the prompt explicitly forbids.

**Fix.** In the fix-prompt, after attempt 3, relax the "signature verbatim"
rule. Allow the model to return a `<SIGNATURE>...</SIGNATURE>` block in
addition to `<BODY>`. If present, we splice the new signature in place of
the old one. Cargo check validates correctness as before.

**Risk.** Model may "improve" signatures that don't need improving. Mitigate
by gating on attempt count (only after attempt 3) and by requiring a
non-empty `<BODY>` alongside.

**Files.** pipeline.py: `_build_per_fn_fix_prompt`, `_locate_fn` splice
extended to handle signature swap, new `_replace_fn_signature` helper.

## P3.2 ⚑ Rust-general rules as static prompt preamble

**Problem.** Six confusion classes recur across runs. Each is rediscovered
via the notes system on every fresh project. Inefficient.

**Fix.** Add a `<RUST_GOTCHAS>` section to the base per-fn prompt template,
inserted between the signature and MODEL_NOTES. Contains the 6 rules above
verbatim. MODEL_NOTES then adds project-specific learnings on top.

**Files.** pipeline.py: `_build_per_fn_prompt`, `_build_per_fn_fix_prompt`,
new constant `_RUST_GOTCHAS_PREAMBLE`.

## P3.3 ⚑ Smarter note dedup — semantic similarity, not just hash

**Problem.** 4 of 17 notes paraphrase the same TypeId rule.

**Fix.** Before appending a new note, compute a normalised signature
(error code mentioned + key symbols mentioned). Reject if the signature
matches an existing note's signature. Keep the older note (it has more
history).

**Files.** pipeline.py: `_append_model_note`, new `_note_signature`.

## P3.4 ⚑ Note relevance filter — error-class-aware ranking

**Problem.** Fix-prompts include 8 notes regardless of relevance.

**Fix.** When loading notes for a fix-prompt, extract the error code(s)
from the current cargo error excerpt. Boost notes whose text mentions
the same error code or its associated keywords (`'static`, `TypeId`,
`dyn`, `NonNull`, etc.). Tier 1 by error-code match, tier 2 by file/symbol,
tier 3 by recency.

**Files.** pipeline.py: `_filter_model_notes_for_prompt`.

## P3.5 ⏳ `rustc --explain` for stuck fns

**Problem.** No canonical guidance for repeated error codes.

**Fix.** After fix-attempt 5, identify the most-recurring error code in the
current fix-loop and append its `rustc --explain Exxxx` output to the
prompt's COMPILE_ERRORS section (as `<EXPLAIN>` block, ~50-200 lines).
Cached per error code.

**Files.** pipeline.py: `_explain_cache`, `_run_rustc_explain`,
`_build_per_fn_fix_prompt` injection.

## P3.6 ⚑ Strip file attributes from prompt view

**Problem.** Top-of-file `#![allow(...)]` etc. eat tokens.

**Fix.** Extend `_annotate_file_view` to strip top-level
`#![...]` attribute lines from the rendered file. Keep them on disk so
the actual splice site is unaffected; only the prompt view is cleaner.

**Files.** pipeline.py: `_annotate_file_view`.

## P3.7 ⚑ Widen cargo error excerpt for indirect failures

**Problem.** Wrong fill in src/error.rs can break src/lib.rs; the lib.rs
errors are filtered out by `_focus_cargo_errors`.

**Fix.** Include errors that mention crate-level or `lib.rs`/`main.rs`
locations even when the target fn's file doesn't appear in them. Tier the
output: errors-in-target-file first, then errors-in-other-files of the
same crate, with a header demarcating the two.

**Files.** pipeline.py: `_focus_cargo_errors`.

## P3.8 ⚑ Diagnose nightly cluster toolchain mis-application

**Problem.** E0554 in nightly cluster → toolchain not "nightly".

**Fix.** Investigation step: print `cluster.toolchain` and the final
`check_cmd` to the log on every cluster start. If `nightly` cluster has
no toolchain set, fix in `task0_cfg_scan` so it produces
`toolchain="nightly"` correctly.

**Files.** pipeline.py: instrument `_generate_one_cluster` startup,
verify `task0_cfg_scan` cluster construction.

## P3.9 ⏳ Annotated-stub fatigue handling

**Problem.** Multi-pass retry attempts annotated stubs multiple times with
the same underlying failure mode, even when prior attempts produced the
same error class.

**Fix.** When an annotated stub fails AGAIN in a later pass with the same
error code as before, skip that fn in subsequent passes within the same
run. Track in a per-run `(file, fn) → failure_error_codes` map; if a new
attempt's first cargo error code matches a code already in that fn's
set from a previous pass, mark the fn truly_stuck and don't retry.

**Files.** pipeline.py: `_task2_fill_fns_one_by_one` state.

## P3.10 ⏳ Per-pass cargo test exercise (re-run after P3.0 wipe fix)

**Problem.** Test phase silently broken all night. Now fixed in code but
not yet exercised — next pipeline run will validate.

**Action.** Restart pipeline after P3.1-P3.8 land. Watch for first real
`phase_c.pass.test rc=0` events. If tests still don't run, escalate.

## P3.11 ⚑ monitor.py — actionable per-fn overview panel

**Problem.** Current monitor view shows TASK phases (t0a/t0b/...) but
nothing about Phase C per-fn progress. After 13 overnight hours, the user
has no at-a-glance answer to "what is happening right now and is it
healthy?". They have to ask Claude to read events.

**Fix.** New `Phase C` panel added to the monitor view (key `1`), populated
from `logs/events.jsonl`. Six metrics from the overnight retrospective
that turn out to actually be measurable and useful:

```
─── Phase C (per-fn) ───────────────────────────────────────────────
cluster: nightly | pass 2/3 | elapsed 3h14m | last event 47s ago

stubs:  ████████░░░░░░  9/13 done · 1 in flight · 3 remaining
fills:  ✓ first-try 6  ↻ recovered 3  ✗ exhausted 4  ⊘ skip 0
recent: ▁▂▃▅▅▆▄▃▂▁ fills/10min (last hour)

body:   median 64c · last 5: [161, 87, 35, 17, 41]
fix:    median 1 attempt · last 5: [4, 2, 1, 1, 2]
notes:  17 in FR  (+5 last hour)

current: object_downcast attempt 14/20 (8min elapsed)
hot fn:  object_drop_front (18 attempts cumulative across passes)
test:    last: rc=101 stems=11 failed=0 (broken — see P3.10)
```

**Implementation.**
- New helper `_parse_phase_c_events(events_path) → dict` returns the
  rolled-up view (current cluster/pass, fills counter, recent fill rate,
  hot fn, last test).
- New panel renderer `_phase_c_panel(state, width) → list[str]`.
- Inserted into the `monitor` view's render flow between the existing
  Pipeline phases row and the Generation panel.
- Refresh on the existing 2-second tick.

**What the panel proves over time.** Beyond at-a-glance status, the
historical data feeds back. Patterns identifiable after a few hours:
- Hottest fn → candidate for prompt-rule injection
- Recovery rate trend → measure of whether notes are helping
- Time-per-fn distribution → identify timeout candidates
- Test phase result trend → confirms P3.10 fix worked

**Files.** monitor.py: new helpers + integration into existing `monitor`
view path.

## Reanalysis — refinements before implementing

### P3.1 — signature relaxation is more nuanced than first written

Reality check: signatures aren't truly locked because the model has to
match them. They're locked because the tests CALL fns by those signatures.
Changing a `pub fn` sig may break tests. Changing a PRIVATE fn sig only
needs to not break in-crate callers — cargo check catches that.

Refined design:
- Allow sig change ONLY for fns whose `_locate_fn` finds no `pub` keyword
  in the sig text.
- Gate by attempt count: not before attempt 5 (give the verbatim-sig
  approach a fair shot).
- Splice the new sig, run cargo check, on failure revert sig AND body to
  snapshot.
- Add to MODEL_NOTES the fact that a sig change was attempted (so future
  attempts on the same fn don't re-try the same change).

### P3.2 — Rust-gotchas preamble — selective injection vs always-on

Always-on (1-2 KB per call × hundreds of calls = ~2-5 MB extra prompt
chars over a full run). Acceptable cost but ALSO drowns the model in
generic rules for fns where they don't apply.

Refined design: TWO levels.
- A short 1-line summary of each gotcha (~15 chars each, 6 lines ≈ 90 chars)
  appears in EVERY fix-prompt. Just enough to remind the model the
  category exists.
- The full multi-line gotcha rule is injected ONLY when the cargo error
  matches the gotcha's trigger (error code or keyword). E.g. "TypeId" in
  error → inject the TypeId rule. Triggered by P3.4's relevance filter.

This way the model always has the cheat-sheet line, and gets the full
text only when it matches the current error.

### P3.3 — semantic dedup — what's the signature?

Simplest signature that catches the 4 TypeId paraphrases:
- The set of important Rust tokens in the note: error code (Exxxx) if
  any, keywords from a curated set (`TypeId`, `'static`, `&dyn`, `Box`,
  `NonNull`, `unimplemented`, etc.), and the verbs used ("tried", "use",
  "consider").
- Two notes are duplicates if their token sets overlap by >=80%.

### P3.4 — note relevance filter — needs the current error in scope

The current `_filter_model_notes_for_prompt` only takes file/fn/file_text.
To rank by error-code match, it needs the cargo error excerpt too. Easy
signature change — add `current_error: str = ""`.

### P3.7 — wider cargo error excerpt — needs cap discipline

Adding cross-file errors could explode the COMPILE_ERRORS block. Cap at
~4 KB total, split as: 60% target file errors, 40% cross-file errors.

### Net refined order

```
P3.6 → P3.7 → P3.8 → P3.3 → P3.4 → P3.2 → P3.1 → P3.11 → restart → P3.10
```

P3.6/P3.7/P3.8: small mechanical fixes, no architectural risk, do first
to keep the diff small per commit.

P3.3 → P3.4 → P3.2: notes pipeline cleanup. P3.4 must come before P3.2
because P3.2's trigger-based injection depends on the error-aware filter.

P3.1: highest-leverage architectural change, do last so we land on top of
a clean note-system.

P3.11: monitor improvement is independent; do alongside pipeline.py
changes since it consumes events those don't break.

P3.5 (rustc --explain) and P3.9 (truly-stuck handling): defer until the
P3.1-P3.4 changes ship and we measure their effect.

---

# Plan 6 — Error-pattern-driven prompt enhancements (2026-05-26 PM)

Source: mid-run analysis of the fresh-start anyhow run after P5.1–P5.5
shipped. Error code distribution after ~50 fills:

```
E0277 (trait bound not satisfied):    11   ← top
E0061 (wrong number of args):          7   ← top
E0308 (mismatched types):              4
E0599 (no method):                     2
E0507 (cannot move out of borrow):     1
```

Top recurring error classes signal specific blind spots in the current
prompt design. The notes system auto-extracts lessons (8 notes this run)
but only AFTER the failure happens — fns processed earlier in the same
pass don't benefit.

## Status legend
⚑ = ready to implement
⏳ = depends on earlier work or measurement
○ = parked (overlaps with existing functionality)

## P6.1 ⚑ Method signatures in TYPES_IN_SCOPE
**Largest expected impact.** Currently the prompt shows:
```
struct Ref<T>
  methods (call with .name()): new, from_raw, cast, by_mut, as_ptr, deref
```
The model knows methods EXIST but not their signatures. When it writes
`Ref::new(self.inner.ptr)` and `Ref::new` takes `&T` (not `NonNull<T>`),
cargo barks E0308. With sigs:
```
struct Ref<T>
  methods:
    pub fn new(ptr: &T) -> Ref<'_, T>
    pub fn from_raw(ptr: *const T) -> Ref<'_, T>
    pub fn cast<U>(self) -> Ref<'_, U>
    ...
```
The model would have picked `from_raw` for the NonNull case.

The syn extractor already emits sigs in MethodRecord.sig — we just
display only names today. Change in `_format_type_for_prompt`. Cap
to ~6 methods per type to keep prompts compact; truncate long sigs.
Estimated impact: directly attacks E0061 + E0308 (~44% of this run's
errors).

**Files:** pipeline.py:`_format_type_for_prompt`. ~15 lines.

## P6.2 ⚑ New Rust gotcha entries (function-pointer fields, move-vs-borrow, NonNull conversions)
Three patterns the model keeps hitting:

1. **Function-pointer fields need parens**: `(s.callback)(args)`,
   not `s.callback(args)`. Vtables in anyhow expose dispatch fns as
   fields; model keeps using method syntax.
2. **Match on owned vs borrowed**: `match self.field { ... }`
   moves. Use `match &self.field { ... }` or `match self { Foo { ref field } }`.
3. **NonNull conversion table**:
   ```
   NonNull<T> → &T   :  unsafe { ptr.as_ref() }
   NonNull<T> → *mut T:  ptr.as_ptr()
   &T → NonNull<T>   :  NonNull::from(&val)
   *mut T → NonNull<T>: unsafe { NonNull::new_unchecked(raw) }
   ```

Add three entries to `_RUST_GOTCHAS` with their trigger keywords.

**Files:** pipeline.py:`_RUST_GOTCHAS`. ~50 lines.

## P6.3 ⚑ Type-specific RECURRING_ERRORS enrichment
Today `<RECURRING_ERRORS>` says:
```
E0277 (seen 11×): trait bound not satisfied — add Send/Sync/'static.
```
The 11 actual E0277 cases this run all involved `Box<dyn StdError +
Send + Sync>`. A smarter version would mine the recent error excerpts
for shared type strings and surface them:
```
E0277 (seen 11×, all on `Box<dyn StdError + Send + Sync>`):
  trait bound not satisfied; the dyn trait object is unsized
  — wrap with Box::new(value) when constructing.
```

Approach: when promoting an error code, also collect substrings of
recent error excerpts that share a type token. Show the top shared
context.

**Files:** pipeline.py:`_promoted_error_rules`,
`_record_error_codes_seen` (now stores excerpts not just counts).
~40 lines.

## P6.4 ⚑ Notes tagged by error code; surfaced alongside RECURRING_ERRORS
Today notes are filtered by file/symbol relevance (P3.4). But if a
note about "fn pointer field syntax" was extracted because the model
hit E0061, it should be surfaced when E0061 RECURS — even if the
file/symbol filter doesn't match.

Approach: when a note is added, tag it with the error code(s) that
triggered its extraction. When emitting `<RECURRING_ERRORS>`, also
emit the relevant past notes for those codes.

**Files:** pipeline.py:`_append_model_note` (add error_codes param),
note storage format (extend with optional `<!--codes:E0061,E0308-->`
suffix or similar inline metadata), `_promoted_error_rules`.
~50 lines.

## P6.5 ○ Mid-pass note propagation (already in place)
Re-examined: `_filter_model_notes_for_prompt` is called INSIDE the
per-fn fix loop's while iteration. So each retry within a fn DOES
pick up newly-added notes. Across fns within a pass, fn B's first
attempt sees notes added by fn A's recovery (because notes are
loaded fresh from FR per call). NOT a problem — verified.

## P6.6 ⏳ Intra-file impl method sigs (defer until measured)
The file context shown to the model is already the full file
content; method sigs for types defined in the file are visible
inline. P6.1 fixes the cross-file case; intra-file is solved by
the file context. Park unless evidence shows the model misuses
same-file methods despite seeing them.

## P6.7 ⏳ Persist cross-run RECURRING_ERRORS context
Today RECURRING_ERRORS resets per run. The 11 E0277s of this run
are forgotten on restart. A persistent counter in
`functional_requirements_{crate}.md` (under a new section like
`## Recurring Errors`) would let restarts benefit from prior runs'
error patterns. Same general approach as Model Notes.

Defer: low marginal value until cross-run state matters more
than within-run learning.

## Order of work
```
P6.1 → P6.2 → P6.3 → P6.4 → measure → maybe P6.7
P6.5 already done. P6.6 parked.
```

Estimated total: ~150 lines of code across these four. Each
independently testable via `bash check.sh` + a sample prompt
inspection.

## What we expect to see after P6.1-P6.4
- E0061 rate drops by ~70% (method sig now visible)
- E0308 rate drops by ~50% (method sig + NonNull gotcha)
- E0507 drops to ~0 (move-vs-borrow gotcha)
- Function-pointer field misuses drop to ~0

If those don't materialise, the gotchas / type info aren't reaching
the model effectively — investigation needed (prompt size, ordering,
priority filter).

---

# Plan 7 — Universal improvement-driven loop (2026-05-26 evening)

Source: systematic audit of every fix/retry loop in pipeline.py after
P1+P5 converted the per-pass test-fix loop to a failure-floor stall budget.
Five other loops still use numeric caps or set-equality stop conditions
that prematurely abandon improving runs.

## Loop inventory

```
ID  Loop                                  File:line    Cap kind         Retro?    Metric
L1  Per-fn cargo-check fix loop           6946         count=10         excerpt   error count
L2  Phase C pass loop                     6789         count=3          via subs  fail count
L3  task0c augmentation loop              15428        count=10         deep      uncov lines
L4  task2 BUILD_CHECK_RETRIES             12367        count=5          excerpt   error count
L5  task3 full test run                   12820        none (one-shot)  none      —
L6  generate_fix_loop                     12550        sigs equality    narrative error sig
L7  Per-pass test-fix loop (Phase C)      7273         floor-stall ✓    classifier  fail count
L8  Outer module loop                     17855        none             genesis   pass/fail
```

L7 was rebuilt this session (P1+P5+P3). L8 is healthy. L5 has no retry
at all — the biggest gap. L1/L2/L3/L4 use raw counters that override the
improvement signal. L6 stops on signature-set change, which is too strict
(error swap counts as "improvement" even when count is unchanged).

## P7.1 ⚑ Helper `_improvement_loop`

```python
def _improvement_loop(
    *, name: str,
    run_check: Callable[[], tuple[bool, str, int]],   # → (ok, output, metric)
    do_fix:    Callable[[str, list[dict]], dict],     # → record
    do_retro:  Callable[[str, list[dict]], str] | None = None,
    max_stalls: int = 3,
    hard_cap:   int = 100,
    on_iter:    Callable[[dict], None] | None = None, # archival hook
) -> tuple[bool, str, list[dict]]:
```

Behaviour:
1. `ok, out, metric = run_check()`; if ok → return.
2. iter += 1. If iter ≥ 2 and `do_retro` given → call it; append to fix
   prompt context.
3. `do_fix(out, history)`; append record; emit `loop.iter` event.
4. `run_check()` again; update floor and stall counter.
5. Break on `stall >= max_stalls` or `iter >= hard_cap`.

Emit schema (`loop.iter`):
```
{name, iter, metric_prev, metric_new, metric_best,
 improved, stalled, fix_summary}
```

## P7.2 ⚑ Migrate L1 (per-fn cargo-check loop)

`PHASE_C_FIX_CAP_PER_FN` becomes `PHASE_C_FN_MAX_STALLS` (default 3) +
`PHASE_C_FN_HARD_CAP` (default 30; legacy `PHASE_C_FIX_CAP_PER_FN` honored).
Metric = `len(_cargo_err_sig(output))` (rustc error count, not set).
do_retro = None initially — the current prompt already contains
RECURRING_ERRORS + model-notes + PRIOR_ATTEMPTS. Revisit after measurement.

## P7.3 ⚑ Add L5 (task3 inline retry)

Currently absent. After task3 cargo test fails:
- Run `_classify_phase_c_test_failures` on the failure set.
- For BAD_FILL refs that map to fns we filled this iteration: snapshot
  the file → revert → re-enter the per-fn fix loop with the test failure
  as context.
- Only after the inline retry budget is exhausted, escalate to task4.
- Guard: never touch fns outside the current iteration's manifest (avoid
  mutating already-passing modules).
Env: `TASK3_INLINE_MAX_STALLS=3`, `TASK3_INLINE_HARD_CAP=10`.

## P7.4 ⚑ Migrate L6 (generate_fix_loop)

Add a count-based metric alongside the existing signature-set exit.
Keep the `_attempt_history` retro narrative (it works). Replace the
"sigs unchanged → break" exit with "error count didn't decrease for
MAX_STALLS iters → break". Treat the existing `max_fix_attempts`
parameter as `hard_cap`.

## P7.5 ⚑ Migrate L4 (task2 BUILD_CHECK_RETRIES)

`BUILD_CHECK_RETRIES=5` is a count on full regenerations (expensive).
Replace with floor tracking against the regeneration's first cargo check:
if a regen made strictly fewer errors than the previous regen, reset
stall to 0; otherwise increment. Hard cap stays at 5 (regen is costly).

## P7.6 ⚑ Migrate L2 (Phase C pass loop)

`max_passes=3` becomes the hard cap. Stall counter against
`len(remaining_stubs) + last_pass_test_failures`. A pass that makes any
strict decrease in either resets stall.

## P7.7 ⏳ Migrate L3 (task0c)

Lowest priority: task0c already has the best retro pattern in the
codebase (deep retrospective with category labels and failed-block
evidence). Only the numeric cap is suboptimal. Migrate after P7.1–P7.6
land, to keep the change set small.

## Order of work

1. P7.1 — write helper + tests with a stub run_check.
2. P7.2 — migrate L1; rerun pipeline on anyhow, compare loop exit reasons.
3. P7.3 — add L5; this is the largest behavioural change (new path).
4. P7.4 — migrate L6 (touches every fix-loop caller).
5. P7.5 — migrate L4 (small surface area).
6. P7.6 — migrate L2 (touches Phase C orchestration).
7. P7.7 — migrate L3 (optional).

## Expected effects

- No loop quits because of an arbitrary numeric cap while it was still
  improving. Every termination is tied to "the failure floor has not
  moved for K iterations" with K configurable per loop.
- One uniform `loop.iter` event stream across the pipeline — monitor.py
  panels stop depending on per-loop naming conventions.
- task3 finally has an inline retry path. Failures bounce through a
  per-fn classifier + per-fn fix loop before escalating to task4 + full
  regeneration.
- generate_fix_loop stops abandoning runs on the first error swap.

## What this plan does NOT do

- Does not touch L8 (the outer module loop) — already healthy.
- Does not change task4 / genesis.md flow.
- Does not introduce a new LLM call inside L1 — the per-fn prompt
  already carries the retro signal. Revisit only if measurement shows
  L1 stalls on classifiable patterns.

---

# Plan 5 — Static cluster-set derivation, cross-cluster shared fills

## STATUS: SUPERSEDED (2026-05-26)

The "good part" of Plan 5 — using static cfg analysis to filter fns per
cluster — was implemented as a focused fix instead of a full plan
rewrite. See `_scan_unimplemented_for_cluster` / `_fn_belongs_to_cluster`
/ `_file_cfg_predicate` / `_fn_cfg_predicates` in pipeline.py. The filter
removes ~30% of wasted LLM calls (skips nightly.rs items from non-nightly
clusters) with zero new failure modes.

The "ambitious part" of Plan 5 — cross-cluster SHARED FILLS with variance
recovery — is parked. Empirical reason: the overnight data showed the
model wrote DIFFERENT bodies for the same fn across clusters when each
got its own per-cluster context. Forcing a single shared body would have
saved more LLM calls but at the cost of per-cluster optimisation.

Quality > raw LLM-call savings, so the focused fix wins.

Reconsider only if:
  - A future project has many clusters with truly identical
    implementations (cfg about features, not std-flavor)
  - LLM calls become the dominant bottleneck again after the focused fix
    is measured in practice

The original Plan 5 design follows for reference. Do not implement
without re-validating the variance-rate assumption.

---

Source: 2026-05-26 morning conversation. After Plan 3 fixes shipped, the
remaining inefficiency in the per-fn fill loop is per-cluster duplication:
each unique fn is currently filled once per cluster (3× for anyhow), even
though most fns have NO cluster-specific behaviour. The LLM does identical
work three times.

Original idea was to ask the LLM "are there variances per cluster?" — but
that introduces a false-variance risk and adds prompt complexity. Better:
derive the cluster-set for each fn STATICALLY from the source + Cargo.toml.
No LLM judgment needed for the variance question.

## What's statically derivable

```
inputs:
  Cargo.toml              → features, defaults, dep requirements
  src/**/*.rs (pre-strip) → #[cfg(...)] attrs on items, modules, files
  syn AST                 → cfg-alternate fns (same name, disjoint cfg sets)

output per fn:
  {
    file, fn_name, sig,
    cluster_set: ["stable", "anyhow_no_core_error"],  # where this fn exists
    cfg_attrs: ["not(error_generic_member_access)"],   # what gates it
    cfg_alternate: false,                              # true if a same-name
                                                       # fn with disjoint cfg
                                                       # exists elsewhere
  }
```

Rules:
- Fn with no `#[cfg]` and no `#[cfg]` on its enclosing mod/file
  → cluster_set = all active clusters
- Fn with `#[cfg(error_generic_member_access)]`
  → cluster_set = clusters whose cfg_set evaluates that predicate true
- Two `fn foo` with `#[cfg(nightly)]` and `#[cfg(not(nightly))]`
  → two entries, each with its own cluster_set, cfg_alternate=true
- Fn in a file gated `#[cfg(...)]` (e.g. nightly.rs has the cfg at file
  level via mod decl)
  → file-level gate propagates to all fns inside

## Architecture

```
Phase A* (NEW, no LLM) — extend skeleton extractor:
  Walk pre-strip source under cluster=ALL (no cfg filter at extraction).
  For each fn item, evaluate its accumulated cfg_attrs against EACH active
  cluster's cfg_set. Emit a per-fn record into
    .skeleton/manifest.json
  with the cluster_set + cfg_attrs.
  Cache across runs.

Per-fn loop iterates over UNIQUE (fn_name, cluster_set) entries:
  for entry in manifest:
    cs = entry.cluster_set
    if len(cs) == 1:
      # cluster-specific fn, fill once in that cluster's filled_dir
      fill_in_clusters([entry.cluster], entry)
    else:
      # shared fn, fill once, splice into all clusters in cs
      fill_in_clusters(cs, entry)

fill_in_clusters(clusters, entry):
  - Build prompt explicitly listing the clusters this fn exists in
  - LLM writes ONE body
  - Splice the same body into every cluster in `clusters`
  - For each cluster: cargo check in its env
  - If all pass: done
  - If some fail: enter variance-recovery loop (below)

variance-recovery loop:
  For each failing cluster C:
    Re-prompt: "your body works in {passing clusters} but fails in {C}
                with these errors: ...".
    Model returns a cluster-specific body for C (or a cfg-gated variant
    of the original).
    Splice into C's filled_dir only, leave other clusters untouched.
    Re-check C.
  If a variance is needed, mark this fn as 'has_variances' in the
  manifest so future passes know to fill it per-cluster.
```

## Implementation status

⚑ = ready to implement
⏳ = depends on earlier step

### Step 1 ⚑ Skeleton extractor emits per-fn cluster manifest
Extend `tools/skeleton_extractor/src/lib.rs` to walk all fn items and
emit a JSON record per fn:
- `file: String`
- `fn_name: String`
- `sig: String`
- `cfg_predicate: String` (accumulated from item, impl block, mod, file)
- `is_pub: bool`

The cluster_set evaluation lives in Python (uses existing `_eval_cfg_pred`
+ `cluster.cfg_set` infrastructure). Writes `.skeleton/manifest.json`.

### Step 2 ⚑ Python reads manifest, computes cluster_set per fn
New helper `_load_fn_manifest(skeleton_dir) → list[FnEntry]`.
For each FnEntry, evaluate `cfg_predicate` against each active cluster's
cfg_set. Returns enriched entries with `cluster_set: list[str]`.

### Step 3 ⏳ De-duplicate by (fn_name, file): merge cfg-alternates
If two manifest entries share fn_name + file but have disjoint
cluster_sets, that's a cfg-alternate. Keep as separate entries with
`cfg_alternate=True`. Both get filled per-cluster (the LLM is told only
the cluster cfg active for that specific alternate).

### Step 4 ⏳ Per-fn loop iterates over unique manifest entries
Replace the current per-cluster outer loop with a single pass over
unique (fn_name, cluster_set) tuples. For each:
- Build the per-fn prompt as before, but with the cluster list in the
  header line: `Clusters: stable, anyhow_no_core_error, nightly`
- Tell the model "this fn must compile in ALL listed clusters under their
  respective cfg sets — do not introduce cfg gates unless your body
  fundamentally cannot satisfy all clusters".
- Splice the returned body into each cluster's filled_dir.

### Step 5 ⏳ Per-cluster validation after shared splice
After splicing into N clusters: cargo check in each. Collect outcomes.
- All pass: emit `phase_c.fn.fill.done` once with `clusters=[...]` in data
- Some fail: enter Step 6

### Step 6 ⏳ Variance-recovery: per-failing-cluster cfg-gated body
For each failing cluster:
- Revert the cluster's body to snapshot
- Re-prompt with the cargo error excerpt for that cluster only
- The fix prompt tells the model: "the body works in {passing} but not in
  {failing} — return a cluster-specific body wrapped in
  `#[cfg(...)]` matching this cluster's predicate, OR a body using only
  the intersection of APIs that all clusters provide."
- Splice the variance body into the failing cluster only

### Step 7 ⏳ Mark variances in manifest
When a fn required a variance, record this in the manifest so subsequent
passes (or restarts) skip the unified prompt and go straight to
per-cluster fills.

### Step 8 ⏳ Phase A cache invalidation
The manifest is computed from pre-strip source, which doesn't change
during the run. Cache key: pre-strip git sha + clusters tuple. Invalidate
on cluster set change.

## Expected impact

```
current per-fn × per-cluster (anyhow, 3 active clusters):
  ~30 unique fns × 3 clusters = ~90 fill iterations
  each iteration = 1-20 LLM calls
  worst case: ~30 × 3 × 20 = 1800 LLM calls

after Plan 5:
  ~30 unique fns (most cluster_set = all) → ~30 fills
  shared ones validated in 3 clusters (cheap cargo check)
  variance-recovery only fires when cargo disagrees (~1-3 fns)
  estimated: ~30 × 3 LLM calls (one fill + 0-2 variance) = ~90-150 LLM calls

savings: 70-90% fewer LLM calls. Wall time drops proportionally.
Note quality should be unchanged (same per-fn prompts, just deduped
across clusters).
```

## Risks / mitigations

1. **A "shared" fill that's actually subtly wrong in one cluster.**
   Mitigation: cargo check in EACH cluster post-splice catches it.
   Variance loop fixes.

2. **cfg-alternates pattern not detected.**
   Mitigation: Step 3 detects same-name + disjoint cfg explicitly.

3. **Manifest cache staleness.**
   Mitigation: pre-strip sha key in cache; rebuild on cluster change.

4. **The model writes a `#[cfg]` gate when the variance loop fires.**
   This is correct behavior (we asked for cfg gating). But it changes
   the on-disk skeleton — different from the current "body only" splice.
   Mitigation: extend `_splice_fn_body` to handle the case where the
   returned body includes a `#[cfg(...)]` wrapper around the fn.

5. **Skeleton extractor emits manifest at extraction time, but Phase A
   currently runs per-cluster.**
   Mitigation: change Phase A to extract once with cluster=None (no cfg
   filter — we already do this) and emit manifest then. Per-cluster
   skeleton dirs become symlinks or copies of the same underlying source.

## Order of work

```
Step 1 (Rust extractor) → Step 2 (Python loader) → Step 3 (dedup)
  → Step 4 (loop refactor) → Step 5 (multi-cluster validation)
  → Step 6 (variance recovery) → Step 7 (manifest update on variance)
  → Step 8 (cache key) → restart → measure
```

Steps 1-4 are the minimum to get the LLM-call savings. Steps 5-7 are
needed for correctness (catching false-shared cases). Step 8 is
optimisation.

Total estimated effort: ~250-350 lines (Rust extractor extension is
~80 lines, the rest is Python refactor). Higher risk than Plan 3 fixes
since it touches the per-fn loop architecture.

## Not started — open until Plan 3 fixes measured

Plan 3 is currently running. Wait one more iteration cycle to confirm
P3.1-P3.4 land the expected convergence improvements. Then start Plan 5
if the LLM-call cost is still the dominant factor.

---

# Plan 2 — skeleton-based reconstruction (4-phase TASK 2)

Source: session 2026-05-25 design conversation. Replaces the monolithic
"generate the whole file in one LLM call" approach with a 4-phase pipeline
that gets around the model's output-budget blowups on large files (anyhow's
src/error.rs, ~35KB, fails with empty responses because thinking eats the
output reservation).

## Architecture

**Phase A — Skeleton (mechanical, no LLM, per cluster).**
Rust-side helper using `syn` parses pre-strip source files. Emits per-file
skeletons containing:
  - Imports verbatim
  - ALL type definitions, public AND private (struct fields, enum variants,
    type aliases, consts WITH their literal values, trait definitions)
  - ALL function/method signatures, public AND private, with body replaced
    by `unimplemented!()`
  - All impl block headers (with where-clauses, generics)
  - Macros verbatim (out of scope for reconstruction validation)
  - Attributes preserved (`#[derive(...)]`, `#[inline]`, `#[cold]`, etc.)
  - `#[cfg(test)]` blocks stripped (test files are separate)

After A: `cargo check`. If fails, route through existing fix-loop. Skeleton
volume ≈ 5-8KB per file (vs 35KB full); whole-crate ≈ 30-40KB.

**Phase B — Plan / group tests (1 LLM call, per cluster, cached).**
Single LLM call. Input: all test files + skeleton. Output: a validated
manifest of test groups:
  ```
  groups:
    - name: "error construction"
      rationale: "tests exercise Error::new / Error::msg / From impls"
      test_files: [tests/test_basic.rs, tests/test_repr.rs]
      target_symbols: [Error::new, Error::msg, ErrorImpl::new, ...]
    - name: ...
  ```
Validation: every test file appears exactly once; target_symbols are all
present in the skeleton. Reject on validation failure, retry once, then
fall back to "one group per test file" if still bad.

**Phase C — Per-group fill (N LLM calls = number of groups, per cluster,
re-runs per iteration).**
For each group, in leaf-first order (sort by ascending intersection size
between this group's target_symbols and all other groups' target_symbols):
  Inputs:
    - The full skeleton (small enough to always fit)
    - The group's test files
    - Already-filled function bodies from prior groups (on-disk source)
    - Instruction: "Replace `unimplemented!()` bodies in the functions
      these tests transitively call. Output full files; do NOT modify
      bodies that are already implemented."
  Output: full-file replacements.
  Post-merge: for each output file, preserve any non-`unimplemented!()`
  body that already exists on disk; only accept model output for functions
  whose disk body is still `unimplemented!()`.

After Phase C: `cargo check`. If fails, fix-loop. Then run tests.

**Phase D — Cleanup (0-1 LLM calls).**
Scan for residual `unimplemented!()` after Phase C. Three buckets:
  - Functions tests don't exercise → leave as `unimplemented!()` (panics
    only at runtime; cargo test won't trigger them).
  - Functions tests exercise but no group filled → one LLM call: "fill
    these stubs; here are the relevant tests".
  - Failed fills → existing fix-loop after `cargo test` fails.

## Re-iteration semantics

When TASK 4 updates genesis.md and we loop back to TASK 2:
  - Phase A: cached per cluster (skeleton is deterministic from pre-strip
    source; doesn't change between iterations).
  - Phase B: cached per cluster (test set doesn't change between iterations).
  - Phase C + D: re-run every iteration with whatever feedback emerged.

Cache invalidation: cluster sentinel files in
`.skeleton_cache/{cluster}/manifest.json` carry input hashes. Recompute
on hash mismatch.

## Integration approach

**Add alongside, don't replace.** New backend `StaticSkeletonTask2Generator`
selected by `TASK2_GENERATOR=skeleton` env var. Default remains current
factory (filelist → llm fallback). This lets us A/B compare on anyhow
without burning bridges; revert by env var if regressions appear.

## Open questions deferred until prototype proves Phase A

- Per-function output format (option ii) if full-file + merge bleeds.
- Phase B fallback structure when LLM grouping rejected twice.
- How to handle macros that have private helper functions inside their
  bodies (they're part of the verbatim macro but reference items that
  may be unimplemented).

## Implementation status

Steps 1-11 IMPLEMENTED in-session. Step 12 (end-to-end smoke) RAN successfully
end-to-end through Phase A + B + C; Phase D was blocked by a bug (see below).
Skeleton compiles, grouping works, fills produce results — architecture is
sound. Three bugs found in the smoke must be fixed before this backend is
usable as a default.

### Step 12 smoke results (2026-05-25, anyhow project)

| Phase | Outcome | Notes |
|-------|---------|-------|
| A | ✓ | 29 files skeletonised, 161KB→140KB, cargo check passed |
| B | ✓ | 10 groups produced, attempt 2 of 2 (attempt 1 omitted tests/compiletest.rs) |
| C | ⚠ | 7/10 groups filled; 3/10 empty response; 5.6KB net filled |
| D | ✗ | Never ran — blocked by Phase C non-zero return (bug #3 below) |

Also surfaced: an earlier race where the standalone CLI deleted
`.skeleton/` while the backend class was running cargo check inside
`.skeleton/{cluster}/`. Fixed in-session by isolating the CLI to
`.skeleton/_standalone/` and adding skeleton scratch dirs to SKIP_DIR_NAMES.
Live pipeline crashed once during this race; subsequent smoke run was clean.

## Bugs to fix (post-smoke)

Implementation phases (executed in this order):

- **Phase F1** = Bug 2 (skeleton extractor filter — cheap, no runtime LLM impact, makes downstream tests cleaner)
- **Phase F2** = Bug 1 (merge direction — must come before any further smoke since it affects every Phase C write)
- **Phase F3** = Bug 3 (Phase D unblocking — needed before any value can come out of cleanup)
- **Phase F4** = Bug 4 (think=False retry on empty — recovers groups that currently lose all work)
- **Phase F5** = Bug 5 (compiletest.rs omission — defensive; lowest priority)
- **Phase F6** = re-run end-to-end smoke; compare results table vs first smoke

Each phase is independently testable. F1 needs only a unit-test pass; F2 a unit
test on the merge function + a Phase A repeat; F3 a re-run of `--phase-d-only`;
F4 needs an actual LLM call so test it together with the F6 smoke.

### Bug 1 — merge direction is wrong (HIGH impact)
`_merge_fills_preserving_existing` operates on `model_text` as base and
substitutes disk-filled bodies in. So when the model emits LESS surrounding
content than the skeleton (e.g., dropping `pub use` re-exports, `mod` decls,
imports, attributes), the merged output loses that content too.

Evidence from smoke: group 9 reduced `src/lib.rs` by 702 bytes — likely
dropped module declarations or pub use lines that would break compile.
Group 5 reduced wrapper.rs, kind.rs, nightly.rs by 100-200 bytes each.

Fix: invert. Start from `disk_text`, walk disk functions; for any function
whose body is currently `unimplemented!()`, look up the same signature in
`model_text` and replace just the body with the model's filled body. All
non-function content (imports, type defs, attrs) stays from disk verbatim.

Location: `pipeline.py:_merge_fills_preserving_existing`.

Detailed steps:
1. Replace the function body. New algorithm:
   - Parse disk_text via the same `fn_re` to get `disk_fns: [(sig, body, start, end)]`.
   - Parse model_text the same way, build `model_by_sig: dict[sig → body]`.
   - For each disk_fn in order, decide:
     * if its body is `unimplemented!()` (use `_is_unimplemented_body`) AND its
       signature appears in `model_by_sig`, replace the body in disk_text.
     * else keep disk verbatim.
   - Construct output by stitching: prefix-before-first-fn-body + replaced
     body + content-between-fns + ... using the offsets.
   - Implementation: walk disk_fns in REVERSE order (so offsets stay valid),
     replace the body slice `disk_text[body_start:end-1]` with model's body
     when applicable.
2. Edge case: if a function the model returned doesn't exist on disk, ignore
   it (we never want the model to invent new functions; the skeleton is the
   structural contract). Log at debug level.
3. Edge case: if disk has an `unimplemented!()` fn whose sig isn't in model,
   leave it as-is. Phase D will retry.
4. Add a unit test in `check.sh` (or alongside existing smoke tests): feed a
   small disk_text with one `unimplemented!()` body and one filled body, and
   a model_text that drops a top-level `use` line — assert the merged output
   still has the `use` line.
5. Sanity: re-run `--phase-c-only` on anyhow (LLM-dependent, deferred to F6
   smoke); inspect a touched file to confirm `pub use` / `mod` decls are
   preserved.

### Bug 2 — skeleton extractor includes test sibling files
`*.gen_tests_*.rs` files are protected test artifacts written by task0c
augmentation. They sit alongside source files in `src/`. The Rust
skeleton extractor walks all `.rs` files under the input dir and emits
skeletons for the gen_tests siblings too, which:
  (a) means Phase C's per-group fill prompts include them in the
      skeleton block (wasting tokens),
  (b) lets the LLM emit "fills" for those test files, which the merge
      then writes back — modifying protected tests.

Evidence: groups touched `context.gen_tests_*.rs`, `lib.gen_tests_*.rs`,
`ptr.gen_tests_1.rs`, `ensure.gen_tests_1.rs`, `backtrace.gen_tests_1.rs`
during the smoke.

Fix: in `tools/skeleton_extractor/src/lib.rs::extract_tree`, add a
filename pattern skip for files matching `*.gen_tests_*.rs`. Also defensive
filter in Python `_extract_skeleton`/`_task2_fill_groups`: refuse to write
to any path that matches the gen_tests pattern.

Location: `tools/skeleton_extractor/src/lib.rs` and Phase C/D writers in
`pipeline.py`.

Detailed steps:
1. In `tools/skeleton_extractor/src/lib.rs::extract_tree`, alongside the
   existing skip list for `target/`, `tests/`, `examples/`, `benches/`,
   add a filename pattern check: `rel_str.contains(".gen_tests_")` → skip.
2. Add a unit test in the Rust crate verifying `extract_tree` does not
   write a skeleton for a file named `foo.gen_tests_1.rs`.
3. Rebuild via `cargo build --release` (setup.sh re-run idempotently
   triggers it).
4. In `pipeline.py::_task2_fill_groups`, before writing back to
   `target_src_dir / rel`, verify that `rel` does not match the
   `.gen_tests_` pattern. If it does, log "[skeleton-C] refusing to write
   to gen_tests artifact" and skip. (Belt + suspenders.)
5. Same defensive check in `_task2_cleanup_residuals` writeback.
6. Sanity after F1: re-run Phase A on anyhow, `ls .skeleton/_standalone/src/`
   — no `gen_tests` files should appear.

### Bug 3 — Phase D blocked by Phase C non-zero return
`_cmd_phase_c_only` returns `0 if not result["errors"] else 7`. When
Phase C has partial failures (any group's empty response counts), it
returns 7. `_cmd_phase_d_only` gates on `rc == 0`, so Phase D never
runs after a partial Phase C. But cleanup is precisely the recovery for
partial Phase C failures — it MUST run regardless.

Same issue in the backend class flow — Phase D should always run
after Phase C, success or partial failure. The 4-phase design relies
on Phase D as the safety net.

Fix: drop the rc-gate. Phase D always runs after Phase C. Final overall
success is based on Phase D's residual count, not Phase C's per-group
errors.

Location: `_cmd_phase_c_only` (return logic) and
`StaticSkeletonTask2Generator.generate()` (flow).

Detailed steps:
1. In `_cmd_phase_c_only`, change `return 0 if not result["errors"] else 7`
   to: log the errors prominently, but always return 0. Callers infer
   success/failure from later phases or external checks.
2. In `_cmd_phase_d_only`, drop the `if rc != 0: return rc` short-circuit
   (or relax it to abort only when rc is a hard failure like 2-6 from
   Phase A/B). Phase C's "partial" failure should pass through.
3. In `StaticSkeletonTask2Generator.generate()`, the current flow already
   runs Phase D unconditionally after Phase C — verify by reading. If it
   does, no change needed there.
4. Sanity: re-run `--phase-d-only` on anyhow. After Phase C reports errors,
   Phase D should still execute and log its residual scan results.

### Bug 4 — empty responses on large-output groups
3 of 10 groups returned empty: `ensure_parsing_fallback`,
`core_error_construction`, `ensure_ui_checks`. Same root cause as the
original `error.rs` failure that motivated this whole redesign: the
model spends its generation budget thinking, runs out before emitting
the FILE block.

Fix: in `_task2_fill_groups`, on empty response, retry once with
`think=False` — mirroring the per-file retry pattern in
LLMTask2Generator and the prior task0c lessons.

Location: `_task2_fill_groups` in `pipeline.py`.

Detailed steps:
1. In `_task2_fill_groups`, wrap the existing `ask(prompt, model=_TASK2_MODEL)`
   call in a small retry loop: first attempt without `think` arg (default
   thinking on), second attempt with `think=False` if first response was
   empty.
2. Use the same `extract_files` + filename-relaxed-match + content-strip
   fallback as the LLMTask2Generator empty-response loop (already battle-
   tested for the same model).
3. Log clearly: `[phase-c] group {gname}: empty response on attempt 1,
   retrying with think=False...`.
4. Keep the existing error-collection (`errors.append(...)`) for cases
   where both attempts fail — Phase D will pick those up.
5. Sanity: F6 smoke; the same 3 groups that failed previously should now
   produce at least some fills (or fail and pass to Phase D cleanly).

### Bug 5 — `tests/compiletest.rs` was omitted by Phase B attempt 1
The LLM grouping omitted `tests/compiletest.rs` on attempt 1; the
validator caught it. Attempt 2 succeeded so this didn't block the smoke,
but the LLM systematically views compile-fail tests (trybuild-style) as
"not really tests" and may keep dropping them.

Fix: pre-list every test file path in the prompt with explicit
"cover ALL of these" instruction. Or post-process: if validation fails,
auto-add omitted files to a "miscellaneous" group rather than retrying.

Lower priority — attempt 2 covered it; the retry-with-feedback works.
Location: `_task2_group_tests` prompt and validator.

Detailed steps:
1. In the Phase B prompt, add an explicit checklist near the top:
   `Test files to assign (you MUST cover EVERY entry exactly once):`
   followed by a bulleted list of test file paths.
2. In `_parse_and_validate_groups`, if validation fails specifically on
   "groups omit N test file(s)" and N is small (≤2), instead of rejecting
   and retrying the whole call, auto-append the missing test files to a
   new "miscellaneous" group. This recovers the call without burning
   another large LLM round-trip.
3. Keep the retry path for major coverage gaps (>2 missing files) — those
   indicate a bad grouping that should be re-asked entirely.
4. Sanity: F6 smoke; confirm `tests/compiletest.rs` lands in some group
   on attempt 1.

| Step | Status | Notes |
|------|--------|-------|
| 1 | ✓ | tools/skeleton_extractor — syn-based, 6 unit tests pass, anyhow skeleton compiles |
| 2 | ✓ | setup.sh builds the binary (idempotent skip if present) |
| 3 | ✓ | `_extract_skeleton()` wrapper in pipeline.py |
| 4 | ✓ | `--phase-a-only` CLI; verified end-to-end on anyhow (29 files, cargo check passes) |
| 5 | ✓ | `_task2_group_tests()` + `--phase-b-only` CLI (LLM smoke deferred — model busy) |
| 6 | ✓ | `_task2_fill_groups()` with leaf-first ordering + merge-preserve + `--phase-c-only` |
| 7 | ✓ | `_task2_cleanup_residuals()` + `--phase-d-only` |
| 8 | ✓ | `StaticSkeletonTask2Generator` class implementing Task2Generator |
| 9 | ✓ | Factory: `TASK2_GENERATOR=skeleton` returns the new backend |
| 10 | ✓ | Disk-existence cache for `.skeleton/{cluster}/` and `groups.json` |
| 11 | ✓ | cargo-check insurance between Phase A→B (with fix-loop on failure) |
| 12 | ⧗ | End-to-end smoke with TASK2_GENERATOR=skeleton on anyhow — pending |

## Implementation steps (reference)

Each step is one commit. After each step, `bash check.sh` must pass and a
short sanity check (described per step) must succeed.

### Step 1 — Rust skeleton extractor crate
Path: `tools/skeleton_extractor/` with Cargo.toml, src/main.rs, src/lib.rs.
Dependencies: `syn = { version = "2", features = ["full", "extra-traits"] }`,
`quote = "1"`, `proc-macro2 = "1"`, `walkdir = "2"`.
Binary: `skeleton-extractor --input <pre-strip dir> --output <skeleton dir>`.
Logic:
  - Walk input dir for .rs files (skip target/, .git/, tests/).
  - For each file, parse with `syn::parse_file`.
  - Transform AST: function bodies → `unimplemented!()`, drop
    `#[cfg(test)] mod ...`, preserve everything else.
  - Emit to output dir, mirroring relative path.
Sanity: run on `tmp/anyhow/.original_structure/anyhow/` (if it exists)
or whatever pre-strip code is available, verify output compiles via
`cargo check` in a temp project that wraps the skeleton.

### Step 2 — setup.sh updates to build the extractor
Add `cargo build --release --manifest-path tools/skeleton_extractor/Cargo.toml`
to setup.sh. Binary lands at `tools/skeleton_extractor/target/release/skeleton-extractor`.
Sanity: rerun setup.sh, confirm binary exists.

### Step 3 — Python wrapper to invoke the extractor
Pipeline.py: helper function `_extract_skeleton(pre_strip_dir, out_dir,
cluster) -> dict` that runs the binary, captures stdout/stderr, returns
{files_written, errors}. Cluster passed as a tag for caching.
Sanity: invoke from a small repl test on the same anyhow pre-strip dir
the extractor consumed in step 1.

### Step 4 — Standalone end-to-end test of Phase A
Add a `--phase-a-only` CLI flag to pipeline.py that runs skeleton extraction
on the current project's pre-strip snapshot, writes the skeleton into
`tmp/{project}/.skeleton/{cluster}/`, runs `cargo check` against it
(creating a temp Cargo.toml wrapper if needed), reports pass/fail.
Sanity: `python pipeline.py --project anyhow --phase-a-only` → all
clusters' skeletons compile.

### Step 5 — Phase B grouping orchestrator
New function `_task2_group_tests(skeleton_dir, test_files, project_dir,
cluster, current_module) -> list[dict]`. Builds a single LLM prompt:
all test files + skeleton, asks for JSON manifest. Validates schema
and coverage. Retries once on validation failure. Falls back to
per-file grouping. Writes result to `tmp/{project}/.skeleton/{cluster}/groups.json`.
Sanity: `--phase-b-only` flag, runs A+B, prints group counts.

### Step 6 — Phase C per-group fill orchestrator
New function `_task2_fill_groups(skeleton_dir, groups, project_dir,
cluster, current_module, functional_req, genesis)`. For each group in
leaf-first order: build per-group prompt, call ask(), extract files,
merge with on-disk preserving filled bodies, write back.
Sanity: `--phase-c-only` flag, runs A+B+C, reports bytes filled per group.

### Step 7 — Phase D residual cleanup
Helper `_task2_cleanup_residuals(project_dir, functional_req, test_files)`.
Scans for remaining `unimplemented!()`, classifies as test-exercised or
not (via grep through test files), emits one LLM call for the exercised
set.
Sanity: `--phase-d-only` flag, runs A+B+C+D, reports residual count
before and after.

### Step 8 — New backend class StaticSkeletonTask2Generator
Implements `Task2Generator.generate()` interface. Calls A, B, C, D in
order. Honors the cluster system. Handles cargo-check insurance between
phases via the existing fix-loop.
Sanity: invoke directly via Python repl; module reconstruction succeeds.

### Step 9 — Factory integration
`_task2_generator_factory()` checks `TASK2_GENERATOR=skeleton` and returns
the new generator. Default behavior unchanged.
Sanity: `TASK2_GENERATOR=skeleton bash continue.sh` on anyhow, full
iteration completes.

### Step 10 — Pipeline state caching
Phase A and B results cached in `tmp/{project}/.skeleton/{cluster}/`
with input-hash sentinels. Cache invalidation on hash mismatch.
Sanity: run twice consecutively; second run skips A and B.

### Step 11 — cargo-check insurance + fix-loop reuse
Between A→B: cargo check on skeleton, fix-loop if fails.
Between C→test-run: cargo check on filled source, fix-loop if fails.
Sanity: artificially break the skeleton, confirm fix-loop repairs it.

### Step 12 — End-to-end smoke + handover update
Run full pipeline with `TASK2_GENERATOR=skeleton` on anyhow. Compare:
  - call count vs current backend
  - largest single output volume
  - empty-response failure rate (target: 0)
  - test pass rate after one iteration
Update handover with results and lessons.

---

# Plan 3 — improve TASK 2 skeleton fill quality (post-F6 follow-ups)

Source: F6 smoke run + observations during it (2026-05-25). Plan 2 implemented
the 4-phase TDD architecture; this plan addresses the remaining quality gap:
some groups silently produce nothing (model returns stubs), others hit
empty-response failures even with `think=False`. Items ordered by impact /
effort ratio (cheapest leverage first).

## Status legend
- ⏳ = waits on F6 results before final design
- ⚑ = ready to implement now, independent of F6 outcome
- ⊘ = deferred (last resort)

## Items

### P3.1 ⏳ — Look at the F6 artifacts before deciding the rest
Mandatory first step. Look at `tmp/anyhow/.skeleton-filled/`:
  - `cargo check` exit + first 30 errors → which interfaces broke
  - per-file count of remaining `unimplemented!()`
  - which target_symbols got real bodies vs. stayed stubbed
  - which gen_tests-equivalent tests would run if we ran `cargo test`
Output of this analysis decides which downstream items are critical vs.
optional.

### P3.2 ⚑ — Switch Phase C to per-function output (HIGH impact)
Today the prompt asks for full file replacements; the merge throws away
everything except function bodies. The model wastes ~30KB of output budget
per call re-emitting structure we discard.

Change: introduce a `<FN sig="…">body</FN>` output format. New extractor on
our side splits on this marker. Merge stays signature-keyed.
Expected effect: 10× smaller per-call output, dramatically fewer empty
responses, easier merge (no need to parse full Rust files for body splice).

Steps:
1. Add `_extract_fn_blocks(response: str) -> dict[sig → body]` helper.
2. Replace Phase C prompt's "Output the source files you modified, full
   file replacement…" with "Output one `<FN sig=...>...</FN>` block per
   function whose body you fill. Do not output anything else."
3. Modify `_task2_fill_groups` to drive merge from this dict directly,
   substituting into disk_text by signature. Reuse the disk-base merge
   logic from F2.
4. Unit-test: feed a synthetic response with two `<FN>` blocks; verify
   merge applies both bodies correctly.
5. Run `--phase-d-only` smoke; expect to see groups that previously
   failed empty now produce fills.

### P3.3 ⚑ — Reduce the skeleton block passed in Phase C prompts
Today the entire skeleton (all files, full content including bodies) is
included in every Phase C prompt. The model only needs SIGNATURES it can
call into. Shrinks input by another 60-80%, freeing more output budget.

Steps:
1. Add `_format_skeleton_index(skeleton_files)` that emits signature-only
   summaries:
     ```
     // src/lib.rs
     pub fn x(...) -> ...;
     pub mod foo;
     struct Foo { a: u32 }
     ```
2. Phase C prompt switches to the index by default; full skeleton only
   for the file actually being worked on (the group's primary targets).
3. Unit test: builder produces signature-only output that's at least 5x
   smaller than the full files for anyhow.

### P3.4 ⚑ — Skip groups whose target_symbols are already filled
Leaf-first ordering means late groups often inherit fully-filled targets
from earlier groups. Currently we still send the LLM call. Wasteful.

Steps:
1. Before each Phase C call: scan `target_src_dir` for `unimplemented!()`
   bodies whose function name appears in `group['target_symbols']`. If
   zero stubs remain, log "skipped — all targets already filled" and
   move on without an LLM call.
2. Log saved-call count at end of Phase C.

### P3.5 ⚑ — Few-shot from prior successful groups
When a group's first two attempts both fail, retry once with 1-2 small
in-context examples extracted from earlier successful groups. The
examples anchor the model on the project's specific style (vtable
patterns, error wrapping, etc.).

Steps:
1. After a successful fill, cache the smallest 2 filled functions per
   group in memory (`successful_examples: list[(sig, body)]`).
2. On a third-attempt retry, include up to 2 examples as
   `// Example successful fill from earlier in this run:` blocks.
3. Tune example count down if prompt overflows.

### P3.6 ⚑ — Smaller group size
Phase B prompt currently says "1-5 test files" per group. Drop to "1-3".
Smaller scope = smaller intended output = less budget pressure.

Steps:
1. Update prompt rule.
2. If a group has >3 tests, the Phase B validator splits it into chunks
   of 3.

### P3.7 ⚑ — Save failing (prompt, response, outcome) triples
Even before fine-tuning, we should log these systematically — they're
the corpus we'd use later (P3.9) and useful for human inspection now.

Steps:
1. On empty-response retry exhaustion, write
   `tmp/{project}/.skeleton/_failures/{group}-{ts}.txt` with the prompt,
   the (empty) response, the model's last seen token-count, attempt
   number. Cap file count to keep disk usage bounded.
2. Add a small `--show-task2-failures` CLI to dump the recent failures
   for inspection.

### P3.10 ⚑ — "Don't regurgitate stubs" instruction (HIGH impact)
F6 revealed a silent failure mode: many groups returned files where every
function body was still `unimplemented!()`. The model "answered" with
non-answers. The new merge correctly filters those out, but the LLM round-
trip was wasted. Tell the model explicitly that omitting is better than
re-emitting a stub.

Steps:
1. In the Phase C prompt's output-format section, add: "If you cannot
   produce a real implementation for a function, OMIT it entirely from
   your output. Do NOT emit `<FN>` blocks whose body is `unimplemented!()`
   or just panics."
2. In the LLM's response parser (`_extract_fn_blocks`): when a block's
   body matches `_is_unimplemented_body`, silently drop it. Already
   filtered indirectly by the merge, but doing it at parse time gives
   clean diagnostics ("group X returned N blocks, K were stubs filtered
   out").
3. Sanity: F7 smoke — silent groups should now either fill something
   real or fail visibly.

### P3.11 ⚑ — Strip macro bodies from skeleton index (HIGH impact for macro-heavy crates)
anyhow's `anyhow!`, `bail!`, `ensure!` macros have large `macro_rules!`
bodies (~3-5KB each). They're preserved verbatim by Phase A (correct —
we don't regenerate macros), but they bloat the skeleton index passed
to every Phase C prompt. The model sees a wall of macro syntax and
spends tokens parsing it, even though no group needs to modify macros.

The `ensure!`-test groups likely fail because the macro definition
itself crowds out the room the model needs to think about runtime
helpers the tests transitively call.

Steps:
1. In `_format_skeleton_index`, replace `macro_rules! name { ...body... }`
   with `macro_rules! name { /* body elided — not modifiable */ }`. Same
   for `pub macro` (Rust 2.0 macros).
2. Real macros stay on disk — only the prompt sees the elided form.
3. Unit test: skeleton-index for a file with a 2KB macro is < 200 bytes
   in the macro portion.
4. Sanity: F7 smoke — `ensure!`-test groups should have noticeably more
   room and potentially succeed.

### P3.8 ✗ REJECTED — Fallback to external LLM
Originally proposed as a final-retry escape hatch to Claude / GPT-4 /
external API. **Permanently rejected** per CLAUDE.md rule 7 (2026-05-25):
the pipeline must solve every reconstruction issue using only the
locally-served model. No external LLM call is allowed under any
circumstance — not as a fallback, not for hard groups, not for cleanup.
When the local model is stuck, the only allowed responses are:
prompt-engineering, architectural change, model fine-tune (P3.9), or
accepting the limitation. Do not re-propose this item.

### P3.9 ⊘ — LoRA fine-tune on accumulated failures
Last resort, after P3.2-P3.8 have squeezed out everything they can and
we have a clean failure corpus (P3.7).

Steps:
1. Curate (prompt, ideal-output) pairs from the failure log. Ideal
   outputs come from either: (a) the prior-successful run for the same
   group on a different cluster, (b) human edits, (c) fallback-model
   output (P3.8).
2. Dequantize Qwen3.6-27B-FP8 to BF16 for training.
3. Train LoRA (target rank 16-32, ~50-100k tokens). Evaluate on held-out
   anyhow groups + at least one other crate (argh) to check for
   project-specific overfitting.
4. Re-quantize merged model. Drop in via `MODEL` env var.
5. Re-run F6 smoke against the fine-tuned model and compare.

## Sequencing

After P3.1: implement P3.2 + P3.3 together (paired root-cause fix for
output budget pressure). Then P3.4-P3.7 in any order — they're
independent. P3.8 only if needed. P3.9 only if everything else maxes out.

---

# Plan 4 — Cluster-scoped reconstruction (Phase A/B/C/D per cluster + merge)

Source: 2026-05-25 design conversation, after CLAUDE.md rules 8 + 9 codified
the cluster framework and quality-over-speed principles. F7 showed the
4-phase architecture works (~81% reconstruction of anyhow, cargo check
passes) but Phase C calls still hit the model's capacity limits because
the prompt must reason about every cfg branch at once.

Plan 4 fixes that by applying task0c's per-cluster discipline to tasks
1-4 (specifically the skeleton backend). Each cluster is reconstructed in
its own scratch dir under its own cfg/env; a cross-cluster merge composes
the final source. Per Principle 1, cluster boundaries come from static
source scan. Per Principle 2, the architecture trades runtime (N clusters
× current work) for deterministic per-cluster scoping.

## Status legend
- ⚑ = ready to implement now
- ⏳ = depends on an earlier step

## Architecture

**Cluster-scoped flow (replaces the current single-pass StaticSkeletonTask2Generator flow):**

For each cluster C (lexicographic order, deterministic):
  Phase A(C): extract a cluster-filtered skeleton → `.skeleton/{C}/`
  Phase B(C): one LLM call grouping C's tests → `.skeleton/{C}/groups.json`
  Phase C(C): per-group fills → `.skeleton-filled/{C}/src/`
  Phase D(C): residual cleanup in C's scratch

After all clusters processed:
  Merge pass (no LLM): compose `.skeleton-filled/{C}/` across all C
    into the canonical `tmp/{project}/src/` with cfg-gated diffs.

  Validation: cargo check + cargo test in EACH cluster's env_extra
    against the merged source. Each cluster must pass.

## Implementation status (2026-05-25)

Steps 1-12 IMPLEMENTED. Step 13 (F8 smoke) in flight.

| Step | Status | Notes |
|------|--------|-------|
| 1 | ✓ | BuildCluster.cfg_true/cfg_false/features/toolchain (auto-expanded bare + feature forms) |
| 2 | ✓ | Rust CfgSet + CfgFilterVisitor + --cfg-true/--cfg-false CLI; 4 new Rust unit tests pass |
| 3 | ✓ | `_extract_skeleton(... cluster=...)` + `_cluster_extractor_args` helper |
| 4 | ✓ | Per-cluster Phase A in `StaticSkeletonTask2Generator._generate_one_cluster` |
| 5 | ✓ | `_filter_tests_for_cluster` + `_eval_cfg_pred` (6 Python unit tests pass) |
| 6 | ✓ | Per-cluster Phase B (one LLM call per cluster) |
| 7 | ✓ | Per-cluster Phase C; cluster-filtered skeleton index for prompt scoping |
| 8 | ✓ | Per-cluster Phase D residual cleanup |
| 9 | ✓ | Per-cluster intermediate cargo check (uses cluster env_extra + toolchain) |
| 10 | ✓ | `_merge_cluster_outputs` cross-cluster Python merge with cfg-gated divergence |
| 11 | ✓ | Per-cluster validation (cargo check in cluster scratch with cluster env) |
| 12 | ✓ | Per-cluster `[{cluster.name}]` log prefixes |
| 13 | ⏳ | F8 smoke launched (4 clusters: anyhow_no_core_error, nightly, stable, std) |

## Design corrections during implementation

**A1.** Initial design had Phase A's cfg-filter dropping items from the
on-disk skeleton. This broke compilation because `use crate::X` imports
in surviving items referenced dropped items. Corrected: keep on-disk
skeleton UNFILTERED (Rust's own cfg evaluator handles per-cluster
compilation correctly because cfg attrs are preserved). Cluster cfg-set
is applied only to the PROMPT view via
`_format_skeleton_index_for_cluster` — the model sees a focused view,
the disk stays compilable under every cluster's env.

**A2.** First version of `_cluster_check_cmd` passed `--features X` for
every active_cfg entry. Wrong for non-feature cfgs (like
`error_generic_member_access` which is set via RUSTFLAGS, not as a
cargo feature). Corrected: don't pass `--features` at all; rely on
env_extra (which `task0_cfg_scan` already computes correctly per
cluster).

**A3.** `task0_cfg_scan(project_dir)` runs against the project's current
src/, but during the smoke that's already stripped. Fixed: backend
falls back to scanning the pre-strip snapshot if the current state
shows no cluster-distinguishing cfgs.

## Implementation steps

### Step 1 ⚑ — BuildCluster carries its complete cfg-set
The existing `task0_cfg_scan` enumerates clusters from source. Extend
`BuildCluster` to carry the complete set of cfg-predicate decisions
that define the cluster: every feature flag's on/off state, every
named cfg (`nightly`, `error_generic_member_access`, etc.) with true/false.

Concrete:
  - Add `cfg_set: dict[str, bool]` to BuildCluster. Key is the predicate
    string as it would appear in `#[cfg(KEY)]` (e.g. `feature = "std"`,
    `nightly`, `target_os = "linux"`). Value is the boolean it evaluates to.
  - `task0_cfg_scan` populates this when constructing each BuildCluster
    — it already knows which features/cfgs are on for each cluster.
  - Also add `features: list[str]` (list of cargo feature names to pass
    via `--features` on cargo invocations) and `toolchain: str | None`
    (e.g. "nightly" or None for stable).

Test: assert that the stable cluster's `cfg_set` contains
`("feature = \"std\"", True)` and `("nightly", False)`.

### Step 2 ⚑ — Rust extractor accepts cfg filter, item-level evaluation
Extend `tools/skeleton_extractor` to drop items whose `#[cfg(...)]`
evaluates to false under the supplied cluster cfg-set.

Concrete:
  - New CLI args: `--cfg-true KEY=VALUE` (repeatable, e.g.
    `--cfg-true feature=std`) and `--cfg-false KEY=VALUE` (repeatable).
    Build a `CfgSet` HashMap from these.
  - New `CfgFilterVisitor` that runs BEFORE `SkeletonVisitor` in
    `extract_file`. For each `Item`, walk its `attrs`, find any
    `#[cfg(...)]`, parse the predicate (`syn::Meta::List`), evaluate
    against the cluster's CfgSet using cargo's cfg rules (`not(...)`,
    `all(...)`, `any(...)`, bare ident, `key = "value"`).
  - If the predicate evaluates to false, drop the item. If absent or
    true, keep.
  - Same evaluation applied to `ImplItem` and `TraitItem` for finer
    granularity inside impl blocks.

Unit tests in the Rust crate:
  - `extract_tree` with `--cfg-true feature=std`: items with
    `#[cfg(not(feature = "std"))]` get dropped.
  - Items with `#[cfg(any(feature = "std", nightly))]`: kept if either
    is true in the cfg-set.

### Step 3 ⏳ — Python wrapper threads cluster through to the extractor
`_extract_skeleton(input_dir, output_dir, cluster: BuildCluster | None)`.
When cluster is non-None, build the CLI args list from cluster.cfg_set
and invoke the binary with them.

Concrete:
  - Two helper functions: `_cluster_cli_args(cluster)` builds
    `['--cfg-true', ..., '--cfg-false', ...]`.
  - Update the subprocess call.

### Step 4 ⏳ — Per-cluster Phase A
Replace `StaticSkeletonTask2Generator.generate()`'s single Phase A call
with a per-cluster loop.

Concrete:
  - For each cluster C in `sorted(clusters, key=lambda c: c.name)`:
    - `prestrip_dir = /tmp/prestrip_{project}_{C.name}/`
    - `git --work-tree={prestrip_dir} checkout {prestrip_sha} -- src`
    - `_extract_skeleton(prestrip_dir, .skeleton/{C.name}/, cluster=C)`
    - `cargo check` in `.skeleton/{C.name}/` with `env_extra=C.env_extra`,
      `--features=C.features`, `--toolchain=C.toolchain`
    - On compile failure: cluster-scoped fix-loop (existing `generate_fix_loop`,
      called with cluster's check command)

### Step 5 ⏳ — Per-cluster test scoping
Determine which test files apply to each cluster. Two parts:
  (a) File-level: parse top-level `#![cfg(...)]` of each test file,
      evaluate against cluster cfg-set, drop file if predicate is false.
  (b) Item-level inside `#[cfg(...)] #[test]` functions: the Phase B
      prompt sees the file content, so the model groups based on what
      it sees. We don't need to strip item-level cfgs for the LLM, just
      for the cargo invocations later.

Concrete:
  - New helper `_filter_tests_for_cluster(test_files, cluster)` returns
    only the files whose top-level cfg predicate is true (or absent).
  - Belt + suspenders: also run `cargo check --tests --features=...
    --target=...` per cluster against a temp project; any file that
    doesn't compile under this cluster is dropped from the cluster's
    test set.

### Step 6 ⏳ — Per-cluster Phase B
`_task2_group_tests` already takes `cluster_name` and `skeleton_dir`.
Call it per cluster with cluster-specific inputs.

Concrete:
  - For each cluster: `groups = _task2_group_tests(
      skeleton_dir=.skeleton/{C}/,
      test_files=filtered_tests_for_C,
      cluster_name=C.name,
      module_name=module)`
  - Output lands at `.skeleton/{C}/groups.json`

### Step 7 ⏳ — Per-cluster Phase C with cluster context in the prompt
Phase C prompt header includes cluster-derived constraints.

Concrete:
  - Header block: `Cluster: {C.name}. Features active: {C.features}.
    Toolchain: {C.toolchain or "stable"}. cfg-set:
    {sorted(C.cfg_set.items())}`.
  - Followed by the same skeleton-index + tests + FR + genesis as today.
  - Target src dir: `.skeleton-filled/{C}/src/` (per-cluster scratch).
  - Per-cluster successful_examples cache (no cross-cluster bleed).

### Step 8 ⏳ — Per-cluster Phase D
Same residual cleanup, scoped to cluster.

### Step 9 ⏳ — Per-cluster intermediate validation
After each cluster's Phase C+D completes, run `cargo check` in
`.skeleton-filled/{C}/` with C's env. If it fails, fix-loop. This
catches cluster-specific compile errors immediately (not at the
final merge step where they're harder to attribute).

### Step 10 ⏳ — Cross-cluster merge pass (no LLM)
Compose per-cluster outputs into one canonical source tree.

Algorithm (per src file path P):
  1. Collect `versions = {C.name: read(.skeleton-filled/{C}/src/P)
                          for C if path exists}`
  2. Parse each version with the Rust syn extractor exposed as a
     "decompose-into-items" helper. Output per version: list of
     (item_kind, item_signature_or_name, item_body_text,
      attached_cfg_attrs).
  3. For each item by signature:
     - All clusters with this item have identical body → emit once
       with the union of their attached cfg attrs (likely empty if
       the item wasn't cfg-gated in any cluster).
     - Bodies differ across clusters → emit each version, gated by
       the cluster's CfgSet expressed as a `#[cfg(...)]` predicate.
       Example: stable_fill ≠ nightly_fill → emit two `fn foo`
       versions, one `#[cfg(not(nightly))]`, one `#[cfg(nightly)]`.
  4. Items unique to one cluster: emit with their original cfg
     attribute (preserved from the per-cluster skeleton).
  5. Non-function items (types, consts, imports, macros): take the
     union, preferring the cluster's cfg-aware emission. Conflicts
     here are rare because Phase A's filter prevents the model from
     touching them.

Output: `tmp/{project}/src/{P}` with all cluster contributions composed.

Concrete: new module-level helper `_merge_cluster_outputs(
  cluster_outputs: dict[cluster_name → src_dir],
  cluster_specs: dict[cluster_name → CfgSet],
  out_dir: Path) -> dict[stats]`.

The implementation can live in Rust (extend skeleton_extractor with a
`merge` subcommand) so we can reuse `syn` for AST handling. Per
Principle 2, syn-based merge is more deterministic than Python regex
splicing.

### Step 11 ⏳ — Per-cluster final validation
After merge, for each cluster: `cargo check + cargo test` with C's
env_extra against the MERGED source tree. Each cluster's tests must
pass under its env. Failures here are real failures.

### Step 12 ⏳ — Per-cluster logs + failure dumps
Every log line gets a `[{cluster}]` prefix. Failure dumps go to
`.skeleton/{C}/_failures/`. Cluster-specific log files
`logs/pipeline_{project}_{C}.log` for deep dives.

### Step 13 ⏳ — End-to-end smoke
F8 = run full skeleton backend on anyhow with TASK2_GENERATOR=skeleton.
3 clusters (stable, nightly, anyhow_no_core_error) × ~10-15 groups per
cluster = ~30-45 LLM calls total (vs F7's 16). Each call has much
smaller scope (one cluster's cfgs only) → expected: drastically fewer
empty responses.

Compare against F7: bytes filled, stubs remaining, cargo test pass
count per cluster.

## Cost model
Current single-pass: ~16 LLM calls per iteration.
Plan 4 per-cluster: ~30-45 LLM calls per iteration (3× cluster count).
BUT each call's scope is ~1/3 the size, so per-call success rate
should be higher. Net wall-clock probably comparable; net success rate
expected substantially higher.

## What this plan deliberately does NOT do
- No external LLM fallback (rule 7).
- No fine-tuning yet (P3.9 in Plan 3 remains last-resort).
- No clever heuristics about cluster ordering or skipping.

---

# Plan 1 — Prompt-quality and budget-discipline improvements (COMPLETE)

Source: systematic prompt audit (session 2026-05-24). All P-items shipped
in commits a7238a5, a3dcaf3, 3fa0f62. Plus follow-on: LLMTask2Generator
empty-response retry. See handover for details.

Items deemed non-issues on inspection (P0.2, P0.3, P2.3, P2.5, P2.6) are
documented in handover. Plan 2 above supersedes most remaining "task2
output budget" concerns by structurally reducing per-call output size.

---

# Plan 8 — GPU-parallel unit-test generation for task0c (2026-05-29)

Goal: raise GPU utilization by issuing N concurrent vLLM generation requests
during task0c augmentation. NOT coverage parallelism — splice+coverage stay
serial. Mirrors PHASE_C_PARALLELISM (LLM concurrent, cargo serialized).

## Decisive facts (measured 2026-05-29, live vLLM)

| Fact | Value | Source |
|---|---|---|
| Scheduler concurrency cap | `--max-num-seqs 4` | vLLM launch args |
| Prefix caching | `--enable-prefix-caching` ON | cache_config_info |
| Hist. prefix-cache hit rate | ~1.8% (1.54M/84.2M tok) | /metrics |
| KV budget gauge | num_gpu_blocks reported 204 — implausible, ignored; measured empirically | /metrics |

## Measured concurrency behaviour (tools/bench_parallel.py, P=4, prefix≈6K, out=200)

| mode | wall | agg tok/s | peakKV | capWait |
|---|---|---|---|---|
| serial x1 | 1.30s | 39.1 | 7.4% | 0 |
| x4 DISTINCT prefix | 31.30s | 7.4 | 31.0% | 3 |
| x4 SHARED prefix | 3.76s | 72.8 | 14.8% | 0 |

Distinct-prefix fan-out THRASHES (prefills serialize, capacity-waits, slower
than serial). Shared-prefix scales clean (~10x, no stalls). Effect grows with
prompt size; real task0c prompts are 60-100K tok.

## Cost model (P = effective parallelism)

```
B = KV budget (tok); per req = prefix p + suffix s + output o; cap = max_num_seqs = 4
distinct:  P_eff = min(4, floor( B / (p+s+o) ))
shared:    P_eff = min(4, floor( (B - p) / (s+o) ))   # marginal/req = s+o only
```

Shared prefix makes P_eff scheduler-bound (4), not KV-bound. ⇒ parallelism is
only safe/fast if concurrent requests share their large prefix.

## Design

D1 — Parallel unit = "N generations sharing a prefix", not distinct files.
  Primary mode: same-file best-of-N (N candidate gen_tests blocks for one
  file's uncovered lines, same prompt prefix, picked by coverage gain). KV-near-
  free, improves coverage/attempt, deterministic selection (max lines moved,
  tie-break stable order).

D2 — Prompt ordering for cache hits: stable content (rules preamble, file
  source, coverage_strategy, invariants) as PREFIX; variable content (target
  uncovered lines, per-candidate seed, retro notes) as SUFFIX. vLLM keys on
  longest matching token prefix.

D3 — Serialized stage unchanged: apply gen_tests sibling + mod decl + quick-cov
  behind one lock, in deterministic order. No target/ collision.

D4 — Shared worker-pool helper: factor the PHASE_C dispatch (pipeline.py ~9888)
  into a reusable pool both phases call. Do NOT copy-paste thread logic into
  task0c. New knob TASK0C_PARALLELISM (default 1), same shape as
  PHASE_C_PARALLELISM.

D5 — Cross-file fan-out (distinct prefixes) is allowed only when prompts are
  small enough that P*(p+s+o) <= B; otherwise it thrashes. Default OFF.

D6 — Intra-attempt staleness accepted (same tradeoff PHASE_C documents): batched
  candidates don't see each other's just-confirmed file_what_worked.

## Open / to confirm

- Re-run tools/bench_parallel.py at PREFIX_TOK=80000 to quantify P_eff and the
  shared-vs-distinct gap at realistic task0c prompt size.
- Consider raising --max-num-seqs (setup.sh / vLLM relaunch) only if shared-
  prefix runs show KV headroom at realistic sizes; today 4 is the cap.
- Connects to the prompt-budget finding: smaller prompts + shared prefix both
  raise achievable P. Shrinking task0c prompts compounds the win.

tools/bench_parallel.py committed as the measurement harness
(size-parameterized via PREFIX_TOK/OUT_TOK/P env).

## IMPLEMENTED (2026-05-30) — split-and-merge, within file (Option B)

Chosen objective: partition the file's uncovered functions across N concurrent
generations and MERGE all results (additive), not best-of-N (which discarded
N−1). Per-block compile via the existing fix-loop; coverage once on the merge.

| Piece | Where |
|---|---|
| `generate_in_parallel(queries)` | pipeline.py — one concurrent ask() per query (threads); responses index-aligned; a failed request → "" not abort; len<=1 short-circuits |
| `_partition_uncovered_lines(content, uncov, n)` | pipeline.py — group uncovered lines by function, distribute units largest-first into ≤n balanced disjoint buckets |
| `_finalize_and_commit(raw, pass_n, base_text)` (nested) | pipeline.py — extracted finalize (allow/prelude inject + reject-0-test) + commit/compile-fix loop; reverts to `base_text` on fail so already-merged blocks survive; shared by single-shot AND split |
| `_subset_prompt(lines)` / `_render_prompt` (nested) | per-subset prompt: same file context (shared prefix), narrowed fn_bodies/target |
| `TASK0C_SPLIT_N` env knob | task0c_augment_module; default 1 = legacy single-shot (default path routed through the same extracted helper) |
| `test_generate_in_parallel`, `test_partition_uncovered_lines` | tests/test_pipeline_smoke.py |

Execution per file/attempt: GENERATE N subset blocks concurrently (GPU batch) →
COMPILE each serially via the fix-loop, survivors accumulate as gen_tests_1..N
siblings → COVERAGE (quick-cov) once on the merge. All cargo/coverage serial
(no shared target/ race). Throughput win = each attempt covers N subsets →
far fewer attempts → fewer total coverage runs.

Known minor: on a merge that adds no coverage, revert restores _raw_text
(removes all merged decls) but only unlinks the last subset sibling; the other
gen_tests_*.rs files are left orphaned (unreferenced, harmless, cleaned by the
later strip). Per-subset success log reuses the full uncov count (cosmetic).

NOT YET VALIDATED end-to-end: only check.sh smoke + unit tests. Default
TASK0C_SPLIT_N=1 leaves current runs unchanged. .env currently sets =4 for the
first validation run; confirm: subset prompts batch on the GPU
(num_requests_running>1), `split-and-merge … merged k/N subset block(s)` logs,
coverage climbs faster per attempt, no orphaned-sibling compile issues.

## Still open (cross-file throughput, deferred)
Cross-file fan-out + batched single coverage measurement was the other fork —
not built. Within-file split-and-merge covers the multi-function-file case;
revisit cross-file if whole-file task0c wall-time is still the bottleneck.

---
# Plan 9 — Feed the model the structure it's guessing (context, not caps) (2026-05-30, REV3)

History: rev1 (registry/freeze-spec) and rev2 (gut outer loop) were both wrong —
the outer loop fires 22x in all history; persistence + fixed spec already exist.
Event data showed the cost+instability is the INNER Phase C loop, and the
dominant fix-prompt errors are STRUCTURAL guessing. This rev targets that.

## Quantified root cause (verified)
Budget SINGLE_CALL_CHAR_LIMIT = 317,952 chars. Fill-path structural caps are
HARDCODED at 8192-era values and never re-derived from the window:
| context | cap | as % of budget | line |
|---|---|---|---|
| per-fn type section | 3,000 chars | 0.9% | 9490 |
| per-fn type count | 6 types | — | 9488 |
| per-impl type count | 12 types | — | 8945 |
| task1 API cap (_API_SURFACE_CAP) | 79,488 | — | 13592 |
| anyhow API surface (ground truth) | 129,041 chars | task1 drops 38% | disk |
| API reaching the fill prompt | ~3,000 | ~2% of the 129K | — |

Dominant fix-loop errors (from logs/prompts): E0277 trait-bound ×1996, E0599
method-not-found ×922, E0046 missing-trait-items ×169, E0053 sig-mismatch ×120,
E0609 no-field ×98. All = model guessing types/traits/signatures it wasn't shown.

Big picture: the model window is large; the pipeline extracts rich ground truth
(129K API, type records incl. crate traits, std trait contracts); but the
prompts that reconstruct bodies are starved by stale hardcoded caps. Fix = feed
the ground truth, budget-derived. No hard caps (those regress session 43 and
lower quality). This is a quality lever: informed attempts converge; uninformed
ones thrash.

## F1 ⚑ Budget-derive ALL per-fn / per-impl caps (systemic — not just types)
Older task2_inner_loop / generate_fix_loop / task4 paths are already
budget-derived; the newer per-fn/per-impl Phase C path shipped hardcoded
8192-era caps and was never migrated. Full inventory to fix:
| cap | hardcoded | line | budget-derived target |
|---|---|---|---|
| per-fn type section | 6 types / 3000 chars | 9488/9490 | high-priority fraction of SINGLE_CALL |
| per-impl type section | 12 types | 8945 | same |
| fix-loop error history | 600 chars/attempt | 9724 | _FIX_ERR_CAP (~31K, ALREADY EXISTS, unused here) |
| failed body in prompt | 1500 chars | 8453/8905 | SINGLE_CALL fraction |
| retro compile errors | 2000 chars | 8454 | _FIX_ERR_CAP |
| per-pass test output | 400 / 3000 | 10132/10176 | _focus_cargo_errors default (4000) → budget |
| crate macros | 6000 chars | 5965 | budget fraction |
Make the TYPE/TRAIT section highest-priority. The 600-char error cap is the
headline: the model can't see the full error it must fix while a 31K
budget-derived constant for exactly this is bypassed. One systemic move: route
every per-fn cap through the existing budget-derived constants.

## F2 ⚑ Route full structural ground truth into per-fn / per-impl fills
For the fn being filled, inject the COMPLETE definitions of every type its
signature / impl header / where-clause / referenced tests touch — fields+types,
methods+signatures — plus the trait contract it must satisfy: std via
_TRAIT_CONTRACTS (exists), crate traits via the extractor's trait records
(required methods + exact sigs). Stop selecting a 6-type slice; include the
fn's full structural neighbourhood. Pull from the baked API surface (the FR
intent), not just the live filtered slice.

## F3 ⚑ Stop truncating the API at task1
129K API < 318K budget — it fits. Raise _API_SURFACE_CAP so task1 synthesizes FR
from the WHOLE API instead of dropping 38%.

## F4 ⚑ Error-targeted structural injection in the fix loop
When a fix-loop error names a trait/type/method (E0277/E0599/E0046/E0053/E0609),
deterministically pull that exact symbol's definition/contract and inject it
into the fix prompt — so the retry is informed, not a re-guess.

## F5 ⏳ Observability: log when a structural section is truncated
Emit a guard (like the context guard) whenever types/trait/API context is
dropped, so starvation is visible and we can measure F1-F4's effect.

## Sequencing & framing
F1+F2 first (the fill — biggest quality lever). F3 (task1, cheap). F4 (fix
loop). F5 (observability, do alongside). Pure quality: give the model the facts
it's guessing wrong → more bodies correct on first try → fix loops converge
because each attempt is informed. NOT speed, NOT caps.

## Risk
Bigger prompts approach the window — but measured budget (318K) holds 129K API +
fn context, and the context guard already protects overflow. Prioritize the fn's
OWN types + trait contract first, expand toward whole-API as budget allows.
Verify F2 doesn't push the average fill prompt past SINGLE_CALL after F1.

Status: IMPLEMENTED 2026-05-30. F1 budget-derived constants (_TYPES_SECTION_CAP,
_TYPES_COUNT_CAP, _FAILED_BODY_CAP) + migrated all per-fn/per-impl caps (types
6/3000→budget, 12→budget, 600-err→_FIX_ERR_CAP, 1500-body→_FAILED_BODY_CAP,
2000-err→_FIX_ERR_CAP, test-out 400/3000→_FIX_ERR_CAP, macros 6000→budget). F2
via generous type-section + existing trait-contract injection. F3
_API_SURFACE_CAP //4→//2 (fits 129K). F4 error-named types injected into per-fn
fix prompts (_types_section_eff). F5 phase-c/types_truncated guards. check.sh
green (mypy+smoke). NOT yet validated on a real reconstruction run.

---

# Plan 10 — One universal test→retro→feedback loop everywhere (2026-05-30)

Goal (user): a single reusable loop — generate → run TESTS → retro → feed the
retro+failure back into the prompt → improvement-stop — used across the whole
pipeline. Kills the fragmentation that lets compiling-but-wrong fills through.

## Situation (verified)
The primitive already exists: `_improvement_loop` (7332) does run_check → on
stall do_retro → retro threaded into do_fix's prompt → improvement-stop (no hard
caps). Its docstring says every fix loop "should go through this function." But
only 2 callers use it (generate_fix_loop 16914, task3 retry 17111). ~14 bespoke
while-loops roll their own with inconsistent signals: per-fn fill = cargo CHECK,
per-impl fill = cargo CHECK, per-pass test-fix = cargo TEST, task0c = coverage.
The compile-only acceptance in the fill loops is why semantically-wrong fills are
accepted and the stall happens with tests failing.

## Target architecture
ONE loop (`_improvement_loop`), with two invariants made universal:
- INV1 acceptance = the unit's TESTS pass, not mere compilation. `cargo check`
  is only a cheap fail-fast pre-gate. The metric for improvement-stall is the
  failing-test count (fall back to rustc error count only while the unit doesn't
  yet compile).
- INV2 the retro lesson + the concrete failure detail (compile errors AND
  assertion/expected-vs-actual) are fed into the next generate prompt, and
  persisted to model notes.

## Pieces
U1 `_run_scoped_tests(target_src_dir, cluster_env, test_names) -> (ok, fail_count,
   detail)` — runs ONLY the unit's own tests (names from the reverse-map), not the
   whole suite. Cheap, reusable. compile pre-gate first, then scoped test.
U2 `_unit_fill_loop(...)` — thin wrapper over `_improvement_loop`: run_check =
   compile pre-gate then U1; do_fix = build prompt (with retro + failure detail +
   F1-F5 context) → ask → splice; do_retro = author lesson (model voice) →
   `_append_model_note` + threaded into do_fix.
U3 Migrate consumers to U2 as thin adapters, in order:
   1. per-fn fill (`_task2_fill_fns_one_by_one` inner loop) — highest volume
   2. per-impl fill (`_phase_c_fill_impl_blocks`)
   3. per-pass test-fix (collapses into U2 — no longer a separate loop)
   4. task0c (metric = uncovered-line count; same loop, coverage validator)
U4 TDD outer loop (`_run_phase_c_tdd`): once fills are test-validated (U2), the
   empty-frontier stall disappears — a fill isn't "done" until its tests pass, so
   wrong fills stay on the frontier instead of becoming untouchable.

## Rollout (safe, gated, sequenced — NOT all-at-once)
- Each migration behind `UNIFIED_FILL_LOOP` (default off) so the running pipeline
  is untouched until a step is validated on a live run, then flipped on.
- Build order = U1 → U2 → migrate per-fn → validate live → per-impl → per-pass →
  task0c. Old loop stays as fallback until its replacement is validated.

## Cost
Scoped tests (per-unit, via reverse-map) not whole-suite, so per-fill validation
is bounded. Compile pre-gate fails fast before paying for a test run.

## Why this is the right solution (per CLAUDE.md hard rules)
Systemic + single-source-of-truth (one loop), deterministic acceptance (tests,
not compile), more constraints (a fill must satisfy its tests). No hard caps.

Status (2026-05-30, AFK run): U1 IMPLEMENTED — `_run_scoped_tests` (single
reusable scoped-test check: pass/fail/timeout + failure-detail tail for prompt
feedback) + smoke test `test_run_scoped_tests`. check.sh green.

U2/U3/U4 (wiring test-based acceptance into per-impl/per-fn fill loops and
migrating to `_improvement_loop`) are STAGED — deliberately NOT deployed on the
unattended run. Reason: the acceptance gate needs a per-cluster fail-count
baseline + revert semantics whose subtle bugs could waste a multi-hour AFK run
on churn or regress a fill. That belongs in an ATTENDED session with live
validation, not a blind deploy. The pipeline was started from t2 on the current
validated code (F1-F5 + per-fn/per-impl F4) — a confirmed improvement.

Next attended step: wire U1 into _phase_c_fill_impl_blocks as a "do-no-harm"
gate — after a block compiles, run U1; if fail_count rose vs the cluster
baseline, revert (the fill broke tests) instead of accepting; feed `detail` into
the next attempt. Then per-fn, then collapse the per-pass loop, then task0c.

---

# Night analysis — 2026-06-02 (anyhow run, git e68befe) — SCIENTIFIC PASS

Method: for each failure class — hypothesis → prediction → test on the night's
prompts/failure-dumps → verdict. Confidence stated. Raw error-code aggregates
proved UNRELIABLE (see meta-finding), so verdicts use the failure DUMPS, which
separate "Errors in target file" from "Errors elsewhere in the crate".

## META-FINDING (most important)
Raw error-code counts across prompts (E0425×151, E0277×218, …) are INFLATED by
cascade: when one fn (or a doc parse error) breaks the crate, every other fn's
fix prompt shows those SAME errors as "Errors elsewhere in the crate (likely
downstream of the target)". So an aggregate count ≠ a failure taxonomy. Use the
per-dump target-vs-elsewhere split. (My first plan ignored this and over-counted.)

## P0 — cluster env not threaded into task0c augmentation — CONFIRMED
Test: read `StaticRustTestAugmentor.test_harness_compiles` → `env=_cargo_env()`
(default), and the no_core gen_tests (request_ref) are gated `#[cfg(anyhow_no_core_error)]`.
Verdict: CONFIRMED. A nightly-only-API test passes augmentation under the wrong
env, is kept, then fails under no_core's real env (E0658). Fix (FR-035): thread
cluster env_extra+toolchain through `test_harness_compiles` / `measure_coverage`
/ `_quick_file_coverage`. Confidence: HIGH.

## #1 — vtable / type-erasure machinery — CONFIRMED, cause refined
Hypothesis: tightly-coupled internals the model can't fill per-fn.
Test: `construct_from_adhoc` body = `Self::construct(message, &ADHOC_VTABLE, …)`;
error E0425 "cannot find value `ADHOC_VTABLE`"; another fn used `&VTABLE_ADHOC`;
NO fn defines either and neither is surfaced.
Verdict: CONFIRMED. Root cause is precise: the per-type vtable is SHARED DATA
referenced by several `construct_*`/`new` fns, but per-fn filling gives it no
owner — so no fn materialises it and they reference it under inconsistent names.
`core::alloc::Global` / `allocate_zeroed` are the same shape (model materialises
an allocation scheme from non-existent/unstable symbols).
Solution (FR-036-compliant, NOT "use Box"): co-design the construct/vtable unit
in ONE prompt so the shared vtable is defined once and referenced consistently;
surface the `ErrorVTable` type + the sibling `construct_*` signatures + in-scope
stable symbols so whatever scheme it picks uses real names. Confidence: HIGH on
cause; MEDIUM that co-design alone suffices (some may be a genuine tests-only limit).

## #2 — "API hallucination" — LARGELY REFUTED
Hypothesis (original): model invents internal helpers like `construct_from_boxed`.
Test: `construct_from_boxed` EXISTS in the skeleton AND is surfaced in the
from_boxed prompt (all 5 `construct_*` shown). The from_boxed failure dump's real
error was `unknown start of token: ` at src/lib.rs:212 — "Errors elsewhere",
i.e. a DOC parse error, not the target. The retro nonetheless labelled it
"hallucinated construct_from_boxed".
Verdict: REFUTED as a standalone class. It was (a) a misattributed downstream
error + (b) folds into the genuine vtable-symbol issue (#1). Real symbols exist
and are surfaced. No "inject the API" fix needed — the API is already there.
Confidence: HIGH.

## #2' — Retro MISATTRIBUTION — NEW, CONFIRMED
The retrospective blamed the target fn (`construct_from_boxed`) when the real
error was downstream (doc parse error elsewhere). This wastes fix attempts and
writes WRONG anti-hallucination notes. The fix loop ALREADY tags errors "in
target file" vs "elsewhere" (it's in the dump header) — the retro ignores it.
Fix: when a stalled fill's errors are ONLY "elsewhere/downstream", do NOT run a
target retro and do NOT write a note; treat it as a downstream block (B3 already
keeps non-regressing fills, so this should rarely revert). Confidence: HIGH.

## #3 — E0277 trait-bound / unsized-T — PARTIALLY VALIDATED
Test: context/new/msg dumps show genuine "Errors in target file: error[E0277]
the trait bound …" and "size for values of type T" (unsized return). Real target
errors, not cascade. Subcategory split (unsized-T vs missing Debug/Display vs
where-clause) NOT yet measured per-dump.
Verdict: GENUINE but UNDER-ANALYSED. Before proposing, do the same per-dump
breakdown. Candidate levers (where-tightening; surface exact skeleton bounds)
unverified. Confidence: LOW on solution; do the breakdown first.

## #4 — E0284/E0282 type-inference — UNVERIFIED
Could be genuine or downstream of #1. Not tested. Re-measure AFTER #1/#2' land,
since cascade inflates these. Confidence: NONE yet.

## #5 — doctest doc corruption — RESOLVED (verify)
Test: live merged lib.rs:212 now has `//!` (clean); extractor binary contains
`normalize_doc_attrs`; only 7/147 error-prompts had the corruption (transient,
early in the run before/around the doc-norm taking effect).
Verdict: FR-033 is working; doc corruption is being fixed. Action: confirm zero
recurrence on the next clean run. Confidence: HIGH it's fixed.

## Ranked, evidence-backed next steps
1. P0 cluster-env (FR-035) — HIGH confidence, clear fix.
2. #2' retro-misattribution suppression — HIGH confidence, small, stops wrong notes.
3. #1 vtable co-design — HIGH on cause, the real reconstruction blocker.
4. #3 — do the per-dump E0277 breakdown BEFORE proposing a fix.
5. #4 — re-measure after 1–3 (likely shrinks via cascade removal).

---

# DESIGN — Behaviour hardening via mutation-kill (re-target task0c) — SPIKE-VALIDATED

## Correction vs first draft
NOT a new task0d. The mutation logic re-targets the EXISTING task0c generation
objective from line-coverage to mutation-kill (coverage kept only as a cheap
"line reachable" pre-filter — a mutant on an unexecuted line can't be killed).
One generation phase, behaviour-driven. task0a/0b reframe "coverage strategy" as
"behaviour/mutation strategy"; strip and task1 unchanged.

## Spike results (2026-06-02, pristine anyhow /tmp/anyhow_mut, cargo-mutants 27.0.0)
- S1 tractable count: 176 mutants total (error.rs 44, ptr.rs 37, ensure.rs 26…).
  Verdict: tractable for an LLM loop. PASS.
- S2 cost + survivors exist: fmt.rs 5 mutants in 12s (~2.4s/mutant → ~7 min for
  all 176). 1 MISSED survivor = `> → >=` in `Indented::write_str` — a real
  behavioural gap the author's OWN strong suite misses. Verdict: fast; survivors
  are genuine missing assertions. PASS.
- S3 black-box reachable: that survivor is on the public Debug path
  (fmt.rs:34-39 formats the chain through `Indented`), so a `{:?}` string
  assertion kills it without reading internals. Verdict: black-box discrimination
  feasible. PASS.

## Validated algorithm (re-targeted task0c, per cluster, pre-strip)
1. Coverage pre-filter (cheap): keep current line-coverage pass as the floor
   (ensures lines are reachable).
2. Mutation pass: `cargo mutants` → SURVIVING mutants = behaviours the suite is
   blind to. (deterministic; cargo-mutants in setup.sh.)
3. For each survivor (black-box): LLM proposes public-API call sequences/inputs
   (sees ONLY public signatures/types — never source, never the mutant diff);
   run each on REAL vs MUTANT builds; an input whose observable result differs
   (return/panic/Err/public-state) is discriminating.
4. Emit a characterization test: call with that input, assert the REAL output.
   It kills the mutant AND encodes the behaviour. gen_tests sibling, gated per
   cluster (FR-032), survives strip.
5. Loop until surviving non-equivalent mutants < threshold or input budget hit;
   equivalent mutants (no discriminating input found) → skip.

## Black-box guards (FR-036 / Hard Rule 11)
LLM never sees source bodies or mutant diffs — only public API + observed
outputs. Discrimination is by RUNNING, not reading. Assertions cover public
observable behaviour only.

## Spike-gated implementation sequence (build only after each spike passes)
- M0 [DONE] spikes S1–S3 above → design feasible. PASS.
- M1 wrapper: deterministic `cargo mutants --list/--json` runner per cluster
  (env+toolchain) → survivor list. Spike: returns survivors for one cluster file.
- M2 black-box discrimination harness: given a survivor + a candidate input,
  build REAL vs MUTANT and compare observable output. Spike: kill the fmt.rs
  `>→>=` survivor with a hand-written `{:?}` assertion (proves the loop's core).
- M3 LLM input-proposal loop (public API only) feeding M2; emit passing test.
  Spike: auto-kill ≥1 survivor end-to-end on one file.
- M4 integrate as task0c objective (coverage floor + mutation-kill metric),
  per-cluster, gen_tests emission. Spike: one full file reaches 0 non-equiv
  survivors and the tests survive strip.

## Remaining plan items — each spike-gated
- P0 cluster-env (FR-035): spike = run `test_harness_compiles` with cluster env
  on a no_core nightly-API test and confirm it's REJECTED. Then implement.
- #2' retro-misattribution: spike = confirm dumps tag target-vs-elsewhere; then
  suppress target retro when errors are elsewhere-only.
- #1 vtable co-design: spike = one prompt filling the construct/vtable unit
  together yields a self-consistent vtable symbol. Then implement.
- #3 E0277 / #4 inference: do the per-dump breakdown spike BEFORE proposing.

---

# SPIKE LOG — 2026-06-02 (continued)

## P0 spike → REFUTED my own root cause (good — caught before implementing)
Hypothesis: task0c augmentation uses default env, so it accepted a nightly-API
(`request_ref`) test for the no_core cluster.
Spike: (a) `_cargo_env()` ALREADY appends cluster RUSTFLAGS (pipeline.py, the
"Apply cluster-specific env overrides" block) — so the cfg DOES flow. (b)
`request_ref` fails E0658 under STABLE regardless of cfg (tested in
/tmp/anyhow_mut). The no_core cluster runs on stable → it would have REJECTED the
test at augmentation, not accepted it.
Verdict: REFUTED. Do NOT implement the "thread cluster env into augmentation"
fix. The only env gap that remains is TOOLCHAIN (_cargo_env sets RUSTFLAGS but
not +toolchain) — relevant to nightly, not this no_core symptom.
Re-diagnosis needed: how does a nightly-API gen_test end up gated
`#[cfg(any(anyhow_no_core_error,...))]`? Likely the gen_tests reattach/merge
assigns the wrong cluster cfg to a test generated under the nightly cluster.
Next spike (before any fix): find which cluster generated context.gen_tests_5
and why its decl carries the no_core gate. UNTIL THEN P0 has no validated fix.

## Methodology note
Two of my plan's root causes (#2 API-hallucination, P0 cluster-env) were refuted
by cheap spikes. Lesson reinforced: do not implement a plan item until its
specific causal claim survives a spike. Confidence ordering for what to build
next is therefore: items whose CAUSE is already spike-confirmed, namely —
mutation-kill design (S1–S3 PASS) and #2' retro-misattribution (seen directly in
the from_boxed dump). #1 vtable cause is confirmed; its FIX (co-design) still
needs a spike. P0/#3/#4 need re-diagnosis spikes first.

## P0 re-diagnosis spike (2026-06-02) — mechanism found, fix not yet pinned
Finding: `request_ref` (nightly API) gen_tests appear in ALL THREE clusters'
`.skeleton-filled` trees (shared, not per-cluster). Some carry an internal
`#[cfg(error_generic_member_access)]` (e.g. error.gen_tests_8 — safe under
stable/no_core); others (context.gen_tests_5, wrapper.gen_tests_5, error.gen_tests_9)
do NOT, so they fail E0658 under stable/no_core. The merged decl then gates
context.gen_tests_5 `#[cfg(anyhow_no_core_error)]` — the WRONG cluster (it needs
nightly). So: a nightly-cluster-generated test using a cfg-gated API is shared to
other clusters without being gated to the cfg it requires.
Fix direction (needs one more spike to pin the assigning code): gate each
gen_test to the cluster/cfg it was generated+compiled under, so it only compiles
where its APIs exist. Candidate sites: gen_tests reattach (`_build_gen_tests_decl`
/ cluster_cfg_attr) and the cross-cluster gen_tests merge. NOT the augmentation
env (that hypothesis was refuted).

## P0 — FULLY DIAGNOSED (spike-validated, both mechanisms)
The per-cluster `.skeleton-filled` trees are each CORRECT: nightly's
context.gen_tests_5 has `request_ref`, gated `error_generic_member_access`;
no_core's has no request_ref, gated `anyhow_no_core_error`. The merged tree
mixes them via TWO collapses:
1. SAVE collapse: `saved_gen_blocks` (pipeline.py:23386) is keyed by `rel` only
   and reads siblings from ONE tree → only one cluster's context.gen_tests_5
   CONTENT survives (nightly's request_ref).
2. MERGE-decl collapse: the per-cluster `mod gen_tests_5` DECLS (each gated to
   its cluster) are merged as same-key `Item::Mod` and collapse to ONE gate
   (no_core). The post-merge reattach (23544) passes NO cfg_attr and skips if a
   decl already exists, so the merge's (wrong) gate stands.
Result: nightly `request_ref` CONTENT under a no_core GATE → E0658 under no_core.

### Fix spec (the change to implement)
Make gen_tests per-cluster end-to-end: collect them per cluster from
`.skeleton-filled/{cluster}/src/`, give each a cluster-unique sibling name (so
content can't collapse), and reattach each gated by THAT cluster's cfg
(`_cluster_cfg_attr`). Then a nightly test is gated nightly and excluded from
stable/no_core; no content↔gate mismatch. Touch points: the save (key by
(rel,cluster)), the reattach (per-cluster name+gate), and exclude gen_tests from
the decl-merge collapse (they're now distinct names). FR-038.
Spike before building: confirm per-cluster sibling names + per-cluster gates
produce a merged tree where each cluster compiles its own gen_tests only.
