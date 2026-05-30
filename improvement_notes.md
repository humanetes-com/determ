# Improvement notes

Running backlog of pipeline improvement ideas. Local file only — NOT a cron /
scheduled / remote routine. Reviewed on request, not on a timer. Newest first.
Status: 💡 idea · 🔬 analyzing · ✅ done · ❌ rejected

---

## IN-4 🔬 Feed the model the structure it's guessing — see Plan 9 (REV3)
Date: 2026-05-30 · The real fix (data-verified). Fill-path structural caps are
hardcoded 8192-era values: per-fn type context = 3000 chars / 6 types = 0.9% of
the 318K budget; task1 drops 38% of the 129K API; the full API never reaches the
fill prompt. Dominant errors (E0277/E0599/E0046/E0053) are the model guessing
types/traits/sigs it wasn't shown. Fix (F1-F5): budget-derive the caps, route
full type+trait-contract ground truth into per-fn/per-impl fills, stop
truncating the API at task1, error-targeted injection in the fix loop. Quality
lever, no caps. Supersedes the earlier (retracted) IN-4 drafts below.

## IN-4 (superseded drafts) 🔬 Make t2→t3→t4 fast/deterministic — see Plan 9 (REVISED)
Date: 2026-05-30 · Source: user thesis (task0c >> task2 stability)

CORRECTION: first draft (registry + freeze-spec) was a misread; retracted after
deep trace. Verified facts: `.skeleton-filled` already persists per-fn fills
(idempotent seed, not nuked by delete_source_files); FR is already frozen across
the retry loop (task1 skipped when phase-valid); fills read FR + Model Notes,
NOT genesis; Phase C already writes self-correction notes to FR on recovered
fixes. So persistence + fixed spec + working feedback already exist. The real
problem: the outer loop burns 2 LLM calls/iter (task4 genesis analysis + regen
coverage_strategy) + delete/re-merge that DON'T reach the fills — expensive
inert churn. Fix = REMOVE that inert work on the tdd path (Q1), route the
failure signal into FR Model Notes instead of inert genesis (Q2), lower fill
temperature (Q3). Lever is removal, not addition. Status: planned, verified,
not built.

## IN-1 💡 Consolidate gen_tests siblings into ONE block per file
Status update (2026-05-30): downgraded. Nested-submodule merge is cosmetic only
(namespaces stay separate); flat merge is the only real win but risky. Decision:
leave as-is unless final-artifact tidiness matters. Orphaned-sibling cleanup from
split-merge reverts is the one real (tiny) fix worth doing separately.

Date: 2026-05-30 · Source: user

Problem: each pass writes a new `foo.gen_tests_N.rs` sibling + a `#[path] mod
gen_tests_N;` decl. Over many passes (and now N subset blocks per split attempt)
a file accumulates gen_tests_1, gen_tests_2, … — noise. Goal: at the end of each
file, ONE unique gen_tests block.

Two ways:
- (a) Merge-on-commit: when a block is approved, fold its tests into the
  existing block. Simple mental model but risky DURING generation —
  cross-pass/cross-subset name collisions (two blocks define `fn test_foo` or a
  helper `fn make()`), import dedup, and a merge that breaks compile undoes the
  isolation that made per-block commit safe.
- (b) Consolidation pass at file end (preferred): keep generation collision-free
  with separate siblings (current behavior), then once the file hits its
  coverage target run ONE merge pass: concatenate all gen_tests_N bodies into a
  single `mod gen_tests`, dedup prelude imports, rename colliding fns
  (suffix/namespacing), compile once to confirm, delete the old siblings+decls.
  Delivers the "one block per file" goal without touching the safe generation
  loop.

Recommendation: (b). Decouples "clean output" from "safe generation." Also
subsumes the split-and-merge orphaned-sibling cleanup (IN-2 below).
Effort: medium. Needs a collision-safe Rust test-module merger.

## IN-2 💡 Feed recovered compile-error lessons back into task0c prompts
Date: 2026-05-30 · Source: error-class analysis (this run)

Observed recurring compile errors in task0c generation (fix-loop recovered all):
| class | example | rustc |
|---|---|---|
| hallucinated std path | `fmt::format_args!` (it's a crate-root macro) | E0433 |
| macro_export from inside crate | calling `ensure!`; fixed by switching to internal `__dispatch_ensure` | (macro abs-path) |
| missing type annotation | `Result<i32, E>` generic with no error type | E0282 |

task0c relies on the compile-fix loop to repair these every time instead of
learning. Phase C has self-authored "Model Notes" (CLAUDE.md). Wire the same for
task0c: on a recovered fix (success at attempt>1), extract the lesson in the
model's own voice and inject it into the next generation prompt for that crate.
Fixes the source, not the symptom. Effort: medium.

## IN-3 💡 Prelude injection may nudge path hallucinations
Date: 2026-05-30 · Source: error-class analysis

Blanket prelude injects `use std::fmt::{self, Display, Debug};` into every block.
Putting `fmt` in scope appears to invite `fmt::format_args` guesses (E0433),
masked by `allow(unused_imports)` only when unused. Consider injecting prelude
items only when referenced, or dropping `fmt::self`. Effort: low. Verify it
actually reduces E0433 before/after. Low priority.
