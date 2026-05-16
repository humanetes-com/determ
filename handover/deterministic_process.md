---
WHAT THIS EXPERIMENT IS REALLY ABOUT
---

The source code rebuilt in each iteration is disposable. It is only a scoring
mechanism -- a way to measure whether the current reasoning process is good enough.

The real output of the experiment is genesis.md.

genesis.md accumulates and compounds across iterations. Each test failure teaches
AI something it missed: a class of signal it was not reading from the tests, a
wrong assumption about what requirements are sufficient, a gap in its reasoning
process. Over time genesis.md becomes a documented methodology for translating any
test suite into functional requirements precise enough to guarantee a passing
implementation.

The hypothesis is that once mature, this methodology is project-independent. It
could be applied to a new test suite and produce correct requirements on the first
or second attempt, without needing to iterate.

Using a large, complex project (agave, a Solana validator client) is intentional.
A small project would surface only a narrow set of failure modes. A large project
with diverse crates, many edge cases, and extensive tests stresses the methodology
against a much wider range of patterns, producing a richer genesis.md faster.

The experiment ends when all tests pass. But the artifact that matters -- and that
should be studied, published, or reused -- is the final genesis.md, not the code.

genesis.md must never reference specific tests, test file names, test failures,
iteration numbers, or the iterative process by which it was built. It is a
self-contained methodology document. A reader with no knowledge of this experiment
should be able to pick it up and apply it to any new project. Every lesson learned
from a failure must be restated as a general rule, stripped of all context about
which test, project, or iteration produced it.

---
LONG-TERM GOAL: ONE-PASS GENERATION
---

"Deterministic" does not mean generating the same bytes every run. It means
reliably converging to a passing implementation. The end state is a process that,
given any test suite, generates correct source code in a single pass -- no
iteration needed.

The loop is a learning device. It runs while the process is not yet mature enough
to build in one pass. When genesis.md is fully mature, the loop is no longer needed.

The loop's convergence condition: the set of known-good spec -> code mappings covers
all tests. Once each module/crate produces passing tests in isolation, the whole
project passes. The loop ends.

---
SETUP
---

We have a full product in any language. We remove all source code, keeping only the
tests and the README.md. The pipeline derives everything it needs -- language,
framework, build system, project structure -- from those two inputs alone. Providing
additional hints (build files, dependency manifests, etc.) would bias genesis.md
toward projects that keep those files, rather than teaching AI to reason from
tests and README alone.

Then we run the following iterative process.

---
SCRIPTS
---

Two Python scripts drive the process:

- claude_interact.py  -- sends a prompt to AI and returns the response
- pipeline.py      -- orchestrates the full loop below

On startup, pipeline.py:
1. Reads README.md, asks AI to extract the single shell command used to run the
   full test suite. That command is used unchanged for every TASK 3.
2. Scans the project for all test files and loads them into memory. This is done
   once. The set of test file paths is also kept to prevent AI from accidentally
   overwriting them in TASK 2.
3. Checks for a leftover manifest from an interrupted previous run and cleans up
   any partial source files before proceeding.
4. Checks for existing tests_output_N.txt files to detect a previous interrupted
   run and resumes from the next iteration automatically.
5. Opens run.log for append. Every log line is written to both stdout and this file,
   providing a full audit trail across restarts.

---
ARCHITECTURE: BOTTOM-UP MODULE INTEGRATION (IMPLEMENTED)
---

"Bottom-up integration" is the standard software engineering term for this
approach: start from leaf modules (no dependencies), verify each passes its
tests, then integrate upward through the dependency graph.

PRE-STRIP PHASE (run once before strip_to_tests.py, with --augment-tests flag):
  TASK 0c: drive every module to 100% unit-test coverage using cargo-tarpaulin.
           For each module: measure coverage, ask LLM to generate tests for
           uncovered lines, compile-fix loop, re-measure. Repeat until 100%.
           Writes .coverage_augmented sentinel when done. This ensures that
           after stripping, every module has a complete and meaningful test suite.

Phase 1 -- Global context (runs ONCE per normal pipeline run):
  TASK 0a: identify language, build system, workspace layout from README and
           test file paths. Produces project_profile.md.
  TASK 0b: build the module dependency graph from manifests (Cargo.toml etc),
           determine topological processing order (leaves first).
  TASK 1:  extract contracts from ALL test files across the whole project.
           Produces one global functional_requirements.md shared by all modules.
           Running TASK 1 globally is essential -- without the full project
           picture a leaf module may be generated with types that conflict with
           what the rest of the project expects.

Phase 2 -- Bottom-up module loop:
  For each module in topological order (leaves first):
  1. Run TASK 2 inner loop: generate source files, then compile-fix loop
     (compile-only check + targeted LLM fix) until code compiles cleanly.
     Locked dependency source is injected as context so the model uses
     exact types and functions already built -- no invention.
  2. Run TASK 3: tests for THIS MODULE ONLY (e.g. cargo test -p <name>).
     If tests pass immediately: LOCK the module and move to next.
  3. If tests fail: run targeted fix loop (generate_fix_loop) -- LLM fixes
     only the broken files, re-runs tests. If still failing: TASK 4 updates
     genesis.md, delete this module's files, retry from step 1.
     Locked modules are never touched.

  Final: run the full test suite to validate the integrated whole.

This eliminates the main class of recurring build failures: type mismatches
between modules generated without knowledge of each other. Progress is
unambiguous -- each module is either passing (locked) or not. A locked module
can never be broken by work on a later module.

---
PIPELINE DESIGN: SOLID PATTERN
---

Each pipeline stage is an ABC with two implementations (Static and LLM) selected
by environment variable in .env:

  WORKSPACE_SCANNER=llm     (WorkspaceScanner: identifies workspace layout)
  TASK1_SYNTHESIZER=llm     (Task1Synthesizer: extracts API contracts from tests)
  TASK2_GENERATOR=llm       (Task2Generator: generates source files)

The LLM implementations use contract-first extraction: every function signature,
type, trait, module path, and asserted invariant is extracted explicitly from the
test files, not inferred behaviorally. This produces a typed API contract that
TASK 2 can implement without ambiguity.

---
ITERATION LOOP (CURRENT -- BOTTOM-UP MODULE BY MODULE)
---

STARTUP (single command: python pipeline.py --project <name>):
  TASK 0a. Run once. Identifies language, build system, workspace layout from
  README.md and test file paths. Produces project_profile.md. Cached -- skipped
  if project_profile.md already exists.

  TASK 0b. Run once. Parses workspace manifests (Cargo.toml etc) to build the
  module dependency graph. Determines topological processing order. Pure
  computation -- no LLM call in static mode.

AUTO PRE-STRIP (runs automatically when .coverage_augmented is absent or coverage < 100%):
  TASK 0c. Per module: measure coverage with tarpaulin, generate tests for
  uncovered lines, compile-fix loop, re-measure. Repeat until 100% coverage.
  Writes .coverage_augmented sentinel with overall coverage %. strip_to_tests.py
  runs automatically after augmentation. Use --augment-tests to force re-run.

GLOBAL (once per run):
  TASK 1. AI receives all test files, README, genesis.md, and project profile.
  Produces functional_requirements.md as a typed API contract document: exact
  module paths, function signatures with argument names and types,
  struct/enum/trait definitions, error types, and asserted invariants.
  If test files exceed context budget they are split into chunks, each processed
  independently (with content-hash cache). A hierarchical synthesis step combines
  all chunk results into one document. Skipped if functional_requirements.md
  already exists.

  TASK 1.5. Static use-statement audit. No LLM. Parses all `use` paths from test
  files and checks they appear in functional_requirements.md. Logs gaps as
  warnings -- does not block the pipeline.

MODULE LOOP (per module in topological order, leaves first):
  TASK 2 inner loop. AI generates source files for this module only. A
  compile-only check runs immediately; if it fails, a targeted fix loop asks
  the LLM to fix only the broken files and re-checks. Retries up to
  BUILD_CHECK_RETRIES times before escalating.

  TASK 3. Runs module-scoped test command (e.g. cargo test -p <name>). If all
  pass: module is LOCKED. Its source is injected as context for dependent
  modules. Move to next module.

  On test failure: targeted fix loop (generate_fix_loop) -- LLM fixes only the
  failing files, re-runs tests. If still failing:

  TASK 4. Analyzes failure, updates genesis.md with general lessons. Build
  failures trigger structural analysis; test failures trigger requirements gap
  analysis. Compares pass count against previous iteration to detect regressions.
  Deletes this module's generated files and retries from TASK 2.

  Genesis audit. Runs after TASK 4 on regression or every 5 total iterations.
  LLM reviews genesis.md for contradictions and redundant rules, resolves in-place.

FINAL:
  Full test suite run across the whole workspace to validate the integrated result.
