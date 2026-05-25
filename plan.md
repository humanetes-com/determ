# Pipeline plans

Two plans live here. Plan 1 (the prompt-quality audit) is COMPLETE — kept
at the bottom as historical reference. Plan 2 (skeleton-based reconstruction)
is the active work.

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
