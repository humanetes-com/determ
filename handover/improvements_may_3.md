# Improvements - May 3

## Bug: TASK 0c has_gen branch asks the model to rewrite a file it cannot see

### Location

`pipeline.py`, `task0c_augment_module`, lines 4208-4284.

### Context

TASK 0c runs a loop of up to MAX_RETRIES (3) attempts per module. Each attempt:
- Phase A: compile check with targeted fix
- Phase B: measure coverage with tarpaulin
- Phase C: generate new tests for uncovered lines and write them to disk

In Phase C, for each source file with uncovered lines, the pipeline checks whether a
`gen_tests` block was already appended in a previous attempt (`has_gen`). The intent is
to avoid duplicate Rust module name errors: Rust does not allow two `mod gen_tests` blocks
in the same file.

### The bug

When `has_gen=True` (line 4210), the pipeline sends this instruction to the model (line 4212):

    "A #[cfg(test)] mod gen_tests block already exists.
    Output the COMPLETE updated file with MORE tests added to that block."

The output format (line 4214) asks for `<FILE path="{rel_path}">` - a full file replacement.

But the prompt only shows `annotated`, which is a focused excerpt of the source file
around the uncovered lines (uncovered line plus 4 lines of context on each side, line 4194).
The existing `gen_tests` block lives at the END of the file, well outside that window.
The model has never seen it.

The model is therefore asked to reconstruct the complete file - including a test block it
was not shown - from a small excerpt. In practice it either silently drops the existing
tests (losing coverage gained in previous attempts), misremembers the block structure, or
produces a file that does not compile. Any of these outcomes makes the next attempt worse
than the previous one, and with only 3 outer attempts the module exits with low coverage.

### Fix

Remove the `has_gen` branch entirely. Always use the additive path: ask the model to
output only a new block, and always append it to the file.

To avoid duplicate module name errors in Rust, use a unique module name on each attempt.
Count the number of existing `gen_tests` modules in the file and increment:

    gen_tests      (first append, no existing block)
    gen_tests_2    (second append)
    gen_tests_3    (third append)
    ...

Rust allows multiple `#[cfg(test)] mod` blocks in the same file with different names. All
their `#[test]` functions are collected and run. The virtual output path `gen_tests_block`
is always used, and the pipeline always appends the returned block to `existing_text`.

The pre-clean regex at line 4117 already uses `mod \w+` so it strips all variants when
starting fresh. The save/reattach regex at line 5023 uses `gen_\w+` so it captures all
variants when saving blocks before the TASK 2 wipe.

---

## Architecture: task0c_augment_module hardcodes Rust behavior in pipeline orchestration

### Context

Every other component in the pipeline that has language-specific behavior follows a clean
architecture pattern:

- An Abstract Base Class defines a language-agnostic interface.
- A `Static*` concrete class handles deterministic, language-specific logic (mostly Rust/Cargo).
- An `LLM*` concrete class handles the same concern language-agnostically by asking the model.

Examples already in the codebase:

    WorkspaceScanner      -> StaticWorkspaceScanner, LLMWorkspaceScanner
    DependencyGraph       -> StaticDependencyGraph,  LLMDependencyGraph
    Task1Synthesizer      -> StaticTask1Synthesizer, LLMTask1Synthesizer
    Task2Generator        -> StaticTask2Generator,   LLMTask2Generator

The factory functions (`_workspace_scanner_factory`, `_dependency_graph_factory`, etc.)
select the implementation at startup based on a `.env` setting, keeping orchestration code
free of language-specific conditionals.

### The problem

`task0c_augment_module` is a standalone function that violates this pattern entirely.
It hardcodes throughout:

- `cargo build -p {module_name} --lib` (Rust/Cargo)
- `cargo test -p {module_name} --no-run` (Rust/Cargo)
- `#[cfg(test)]` (Rust attribute syntax)
- `mod gen_tests` (Rust module declaration)
- `use super::*;` (Rust import syntax)
- `#[test]` (Rust test attribute)
- The entire strategy of appending inline test modules to source files (Rust-specific;
  Python uses separate test files, JavaScript uses separate `.test.js` files, etc.)

This means TASK 0c only works for Rust projects. Any other language gets either broken
output or is silently skipped. It also means the orchestration layer (main loop, sentinel
logic, retry logic) is entangled with Rust-specific implementation details.

### Fix

Introduce a `TestAugmentor` ABC following the existing pattern:

    class TestAugmentor(ABC):
        @abstractmethod
        def measure_coverage(self, project_dir, module_name, module_dir) -> tuple[float, list]:
            ...

        @abstractmethod
        def generate_additional_tests(self, project_dir, module_name, rel_path,
                                      existing_text, uncov_lines) -> str | None:
            ...

        @abstractmethod
        def compile_check(self, project_dir, module_name) -> tuple[bool, str]:
            ...

A `StaticRustTestAugmentor` concrete class encapsulates all Rust/Cargo/tarpaulin specific
logic including the unique module naming fix above.

An `LLMTestAugmentor` concrete class handles language-agnostic coverage augmentation by
asking the model to produce additive-only test code with no hardcoded syntax assumptions.
The model sees the source language from the file content and produces appropriate test
structure. The pipeline appends whatever is returned.

A factory function selects the implementation from `.env` (e.g. `TEST_AUGMENTOR=static`
or `TEST_AUGMENTOR=llm`), consistent with how every other component is selected.

The orchestration code in `task0c_augment_module` (retry loop, sentinel logic, pre-clean,
save/reattach) stays in the function but calls only the abstract interface, with no
knowledge of Rust.

### Design note: compile_check scope in StaticRustTestAugmentor

`compile_check` in `StaticRustTestAugmentor` must remain per-crate, not whole-workspace.
The reasons are specific to when TASK 0c runs in the pipeline:

1. Generated tests use `use super::*;` — they access only items inside the current crate.
   They never import from sibling workspace crates, so per-crate compilation is sufficient
   to validate them.

2. TASK 0c runs before stripping. All sibling crates still have their real source on disk,
   so per-crate compilation of the target module works. But the pipeline cannot assume the
   whole workspace compiles cleanly at this point — it is not TASK 0c's responsibility.

3. Whole-workspace validation is TASK 3's responsibility. TASK 3 runs the full test
   command (e.g. `cargo test`) after TASK 2 has regenerated the module source. That is the
   correct and intended point to verify cross-crate correctness.

The `compile_check` contract in the ABC therefore means: "verify that the test harness for
this module compiles in isolation." The `StaticRustTestAugmentor` fulfils this with
`cargo test -p {module_name} --no-run`. The `LLMTestAugmentor` fulfils this by asking the
model to verify syntax without an external tool.

---

## Bug: Chunked TASK 1 extraction loses cross-chunk type information

### Location

`pipeline.py`, `StaticTask1Synthesizer._chunked`, lines 2265-2398.
Constants: `CHUNK_CHAR_LIMIT = 20_000`, `SYNTHESIS_GROUP_SIZE = 10`.

### Context

When the total size of test files exceeds `SINGLE_CALL_CHAR_LIMIT` (20,000 chars), TASK 1
splits them into chunks and processes each chunk independently, then synthesizes the results
in a two-stage hierarchy:

    Stage 0: per-chunk extraction  (one LLM call per chunk)
    Stage 1: group synthesis       (merge SYNTHESIS_GROUP_SIZE=10 partials per call)
    Stage 2: final synthesis       (merge all group summaries into functional_requirements.md)

### The problem

Tests are not independent units of specification. A single behavior is frequently spread
across multiple chunks:

- A type (struct, enum) is constructed in tests in chunk 1 but its fields are only fully
  revealed by tests in chunk 3.
- An error variant appears in chunk 1 but its Display format is only tested in chunk 2.
- A function's edge-case behavior is only visible when comparing tests across chunks.

Each per-chunk model call is explicitly told "there are more test files not shown here"
(line 2448). It can only extract what it sees. The partial requirement for chunk 1 says
"Config is used with some fields" without knowing what fields chunk 3 reveals.

The two synthesis stages compound the loss. Stage 1 condenses 10 partials into one summary.
Stage 2 condenses all summaries into the final document. At each level, specific field names,
variant names, and invariants that appeared only once in one partial are at risk of being
treated as noise and dropped. The final `functional_requirements.md` arrives at TASK 2
with gaps that were present in the original tests but never captured completely by any
single chunk.

TASK 2 then generates code against these incomplete requirements, producing type definitions
that are internally inconsistent or missing fields, which the tests immediately reject.

### Fix

Two complementary mitigations:

1. **Accumulating context header**: pass a rolling summary of types and interfaces already
   extracted from previous chunks into each subsequent chunk's prompt. Each chunk call adds
   to this summary before processing the next chunk. The model for chunk 3 sees "types seen
   so far: Config {timeout, retries}, ParseError {InvalidInput, UnexpectedToken}" and can
   correctly extend rather than independently reinvent.

2. **Conflict detection in synthesis prompts**: the Stage 1 and Stage 2 synthesis prompts
   currently ask the model to deduplicate and merge. They should also explicitly ask it to
   flag contradictions: "if two chunks describe the same type differently, list both versions
   and mark the conflict." The final synthesis then has a signal to resolve rather than
   silently picking one version.

---

## Bug: TASK 0c loop is a fixed 3-attempt for loop, not a coverage-driven while loop

### Location

`pipeline.py`, `task0c_augment_module`, line 4134.
Constant: `MAX_RETRIES = 3`.

### Context

TASK 0c's goal is to drive coverage to 100%. Its main loop is:

    for attempt in range(1, MAX_RETRIES + 1):

This runs at most 3 times regardless of coverage reached.

### The problem

The loop is not driven by the coverage result. It does not keep going until 100% is
reached. The only early exits are:

- `return 100.0` at line 4163 if Phase B already measures 100%
- `break` at line 4186 if tarpaulin reports no uncovered lines in module source files
- `continue` at line 4157 if Phase A compile check fails, burning that attempt with zero coverage progress

A compile failure in Phase A consumes one of the 3 attempts without making any progress.
In a realistic run:

    Attempt 1: compile ok, measure 20%, generate tests (tests may be broken)
    Attempt 2: compile fails (broken tests from attempt 1), continue - zero progress
    Attempt 3: compile ok after 1 fix attempt, measure 40%, generate more tests
    Loop ends. Final tarpaulin run (line 4289) measures 40%. Function returns 40%.

The module is then marked as augmented at 40% (line 4940 in the outer loop). The outer
20% re-augmentation threshold (line 4924) does not trigger, so the pipeline proceeds with
a module whose tests cover only 40% of the implementation. TASK 1 extracts requirements
from an incomplete test signal, and the reconstruction quality degrades accordingly.

Additionally, the final tarpaulin run at line 4289 is redundant: it re-measures coverage
that Phase B already measured in the last completed attempt, adding a full tarpaulin
invocation with no new information.

### Fix

Replace the fixed for loop with a coverage-driven while loop with a separate max-iteration
guard:

    max_attempts = 10
    attempt = 0
    coverage = 0.0
    while coverage < 100.0 and attempt < max_attempts:
        attempt += 1
        # Phase A: compile check
        # Phase B: measure coverage -> if 100.0, return
        # Phase C: generate tests for uncovered lines

This way compile failures cost one iteration but do not permanently reduce the budget.
The loop continues as long as coverage is below 100% and the iteration cap has not been
hit. The redundant final tarpaulin run at line 4289 can be removed since the last Phase B
measurement is already the final coverage.

---

## Bug: TASK 0c generates tests file-by-file without cross-file type context

### Location

`pipeline.py`, `task0c_augment_module`, lines 4188-4284.

### Context

Phase C of TASK 0c iterates over every source file that has uncovered lines and issues one
LLM call per file. For each file the model receives:

- A focused annotation of that file's uncovered lines plus 4 lines of context
- Public symbols extracted from that file only (`_extract_public_symbols(existing_text)`, line 4196)
- Existing tests from that file only (`_extract_existing_tests`, line 4202)

### The problem

A module is not a single file. Functions in `lib.rs` regularly accept or return types defined
in `utils.rs`, `error.rs`, or other submodules. When the model generates tests for uncovered
functions in `lib.rs` it sees those types appear in function signatures (from the annotated
lines) but never sees their definitions. `pub_symbols` at line 4196 is extracted only from
the current file - it does not follow imports or gather symbols from other files in the module.

The model must guess how to construct those types. It will typically guess field names and
field types wrong, producing test code that fails to compile. The compile fix loop at Phase A
then tries to repair these guesses with one fix attempt, which is rarely sufficient when the
root cause is an unknown struct definition.

This is structurally identical to the TASK 2 LLMTask2Generator file-by-file problem: each
file is generated in isolation, causing silent type inconsistencies across files.

### Fix

Before the per-file loop, collect the full content of every source file in the module that
is NOT being targeted in this call. Pass this as a "module context" section in each file's
prompt so the model can see type definitions from sibling files. For large modules this can
be limited to files that are actually imported by the target file (by scanning `use` and
`mod` declarations in the target's content).


---

## Systemic: pipeline-wide context blindness in fix loops and TASK 4

### Overview

The user's concern is correct and applies broadly. Several points in the pipeline give the
model a deliberately restricted or distorted view of its inputs. This is not always wrong -
context limits are real - but the current truncation and filtering choices create active
distortions, not just omissions.

### Blinker 1: generate_fix_loop truncates source files to 2000 chars each

`generate_fix_loop` is the shared repair routine used by TASK 0c (Phase A), TASK 2 inner
build check, and the post-TASK 3 fix loop. At lines 3447-3451:

    if len(content) > 2000:
        content = content[:2000] + "\n// ... (truncated)"
    source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'
    if len(source_block) > 12_000:
        break  # stay within context budget

A typical Rust source file is 200-500 lines, which is 6,000-15,000 chars. The model sees
only the first ~60 lines. If a compiler error points to a function at line 150, the model
is shown the file header and import block, not the broken code. It is asked to fix an error
in code it has never seen.

Worse: the model writes a fix without knowing what comes after the truncation point. It may
emit a type definition or function that already exists 100 lines in, causing duplicate
declaration errors. The "fix" adds new errors.

The 12,000 char total cap has the same problem: files that appear after the cap are not
shown at all. The model produces fixes for errors in invisible files and leaves those files
unchanged.

### Blinker 2: generate_fix_loop provides no functional requirements, no test files, no genesis.md

The fix prompt (lines 3453-3465) contains only the error output and the current source files.
The model has no access to what the code is supposed to do. When used by TASK 0c (Phase A),
`extra_context` is empty. When used by `task2_fix_loop` (line 3514-3516), `extra_context`
adds the functional requirements but they are truncated.

The model is handed a compiler error and source files with no spec, no tests, and no
understanding of what the system does. Correct targeted fixes in this context require guessing.

### Blinker 3: TASK 4 does not receive the generated source files

TASK 4 (lines 3602-3750) receives genesis.md, functional_requirements.md, and the test
output. It does NOT receive the source files that were generated by TASK 2.

TASK 4's job is to identify why the requirements failed to produce a working implementation
and update genesis.md accordingly. But the failure could be in two places:

1. TASK 1 extracted wrong or incomplete requirements from the tests.
2. TASK 2 generated code that does not match the requirements.

Without seeing the generated code, TASK 4 cannot distinguish these cases. If TASK 2 simply
hallucinated a wrong type definition that contradicts the requirements, TASK 4 will
incorrectly blame the requirements and update genesis.md to add a constraint that was
already there. The wrong update makes the next iteration worse.

### Blinker 4: TASK 1 chunk prompt does not include genesis.md

The per-chunk extraction prompt (lines 2443-2462) receives the README and a slice of test
files. It does NOT receive genesis.md. The methodology accumulated across iterations is
therefore not applied during raw extraction - the most important step where signal is
captured or lost. Genesis.md is only applied at the Stage 2 final synthesis, after
information has already been lost through chunking and group merging.

A chunk prompt that includes genesis.md would produce partials that are already
methodology-shaped, reducing the synthesis burden and preserving more signal through
the hierarchy.

### Fix direction

1. For fix loops: show the full relevant file (not 2000 chars), determined by which file
   the compiler error mentions. Show only that file in full rather than all files truncated.
   Include the functional requirements in full for context.

2. For TASK 4: include the generated source files that failed, so the model can distinguish
   "requirements gap" from "generation error." This makes genesis.md updates accurate.

3. For TASK 1 chunks: include genesis.md in the chunk prompt so the methodology guides
   extraction from the first step, not just the last.

---

## Bug: TASK 4 truncates the middle of genesis.md, dropping the most specific lessons

### Location

`pipeline.py`, `task4`, lines 3602-3603.
Constant: `TASK4_DOC_LIMIT = 20_000`.

### Context

TASK 4 analyzes test failures and updates `genesis.md` with general methodology lessons.
Before building the prompt it truncates both genesis.md and functional_requirements.md
using `smart_truncate`:

    genesis = smart_truncate(read(genesis_path), total_limit=TASK4_DOC_LIMIT,
                             head=TASK4_DOC_LIMIT // 4, tail=3 * TASK4_DOC_LIMIT // 4)

`smart_truncate` keeps the first 25% (5,000 chars) and the last 75% (15,000 chars),
dropping everything in between with a "[... N chars omitted ...]" marker.

### The problem

genesis.md grows with each iteration. Early sections contain general methodology principles
(written first, rarely changed). Later sections contain specific, hard-won lessons from
failures on this particular project. Over many iterations, the document grows large enough
that the middle gets truncated.

The middle of genesis.md is not a no-man's-land. It contains the specific lessons added
in iterations 5-20: exactly the accumulated knowledge most relevant to the current failure.
Dropping it means TASK 4 updates genesis.md without knowing what was already tried,
potentially reintroducing lessons that were already there or that were explicitly reversed
because they made things worse.

The failure log (fed separately into TASK 4) partially compensates, but it records
outcomes, not the reasoning in genesis.md itself.

### Fix

Do not truncate genesis.md by position. Genesis.md is a document the pipeline itself
writes and controls - it should be structured to stay within limits, or the limit should
be applied differently. Two options:

1. Raise `TASK4_DOC_LIMIT` to match the actual model context window. If using Claude with
   a 200k context, a genesis.md of 40,000 chars is not a problem.

2. If truncation is unavoidable, truncate by relevance rather than by position. Ask the
   model in a pre-pass to summarize which sections of genesis.md are most relevant to the
   current failure type ("build" vs "test"), and include those sections in full rather
   than keeping an arbitrary head/tail split.

---

## Bug: TASK 0a profile written to pipeline root, shared across all projects

### Location

`pipeline.py`, `main`, line 4830.

### Context

TASK 0a runs once at startup to identify the project's language, build system, and workspace
layout. The result is written to `profile_path` and reused on every subsequent run.

### The problem

`profile_path` is built at line 4830 as:

    profile_path = root / PROJECT_PROFILE_NAME

where `root = Path(__file__).parent` (line 4789) - the directory where `pipeline.py` lives,
which is the pipeline directory itself, not the project directory.

All projects therefore share a single `project_profile.md` at `/home/user/determ/project_profile.md`.
When the pipeline is run on project B after project A, line 2030-2034 finds the existing file,
loads project A's profile, and returns it unchanged. TASK 0a never detects that it is being
applied to a different project.

Every downstream task (TASK 1, TASK 2) receives a profile describing the wrong project. TASK 2
generates source files for the wrong language or workspace layout. The failure is silent because
the profile is never validated against the current project directory.

### Fix

Place the profile inside the project directory, not the pipeline root:

    profile_path = project_dir / PROJECT_PROFILE_NAME

This gives each project its own profile. A profile written during a run of project A is never
visible when running project B. No other changes are needed - the `clean_state` function at
line 4434 already deletes `root / PROJECT_PROFILE_NAME`; update it to use `project_dir` as well.

---

## Bug: TASK 0a profile is never invalidated after project changes

### Location

`pipeline.py`, `task0_project_profile`, lines 2030-2034.

### Context

After the profile-path fix above, each project has its own profile. But the cached profile
is still reused unconditionally across pipeline restarts for the same project.

### The problem

The check at lines 2030-2034 is:

    if profile_path.exists():
        content = profile_path.read_text().strip()
        if content:
            return content

There is no staleness check. If the project's Cargo.toml or directory structure changes
between runs (new crate added, crate renamed, layout reorganized), the cached profile
describes the old structure and TASK 2 generates files in the wrong locations.

More critically: if an earlier run produced a wrong profile (the model hallucinated a module
or build system), restarting the pipeline cannot fix it - the wrong profile is loaded
immediately and poisons every subsequent task.

### Fix

Add a staleness signal. The simplest is a hash of the inputs (README + test file paths)
stored alongside the profile. On load, recompute the hash and regenerate if it changed:

    inputs_hash = sha256((readme_text + test_paths).encode()).hexdigest()[:16]
    profile_inputs_path = profile_path.with_suffix(".inputs_hash")
    if profile_path.exists() and profile_inputs_path.exists():
        if profile_inputs_path.read_text().strip() == inputs_hash:
            return profile_path.read_text().strip()  # still valid
    # regenerate...
    profile_inputs_path.write_text(inputs_hash)

The `--clean` path already deletes the profile file; it should also delete the hash file.

---

## Bug: TASK 0a model receives only file paths, not test file content

### Location

`pipeline.py`, `task0_project_profile`, line 2039.

### Context

The prompt for TASK 0a shows the README and a list of test file paths:

    test_paths = "\n".join(rel for rel, _ in test_files)

The content of the test files (the `_` discarded at line 2039) is never passed.

### The problem

The model infers the project structure from path names alone. For a path like
`argh_shared/tests/lib.rs`, it must guess:
- What language features `argh_shared` exposes (from the name only)
- Whether `tests/` contains integration tests or unit tests
- What `Cargo.toml` fields govern this crate's type

The profile answers "minimum required files for each module" (prompt line 2055) without
seeing any actual import or use statement. If the model guesses wrong about crate types
(lib vs proc-macro vs binary), TASK 2 generates the wrong file scaffold and the build
fails before any test runs.

Showing content is not necessary for all files. The first 500 chars of each test file are
enough to reveal the use statements and test patterns that distinguish crate types.

### Fix

Pass a short excerpt of each test file content alongside the path:

    test_paths = "\n".join(
        f"{rel}\n  (first 500 chars):\n  {content[:500].replace(chr(10), chr(10)+'  ')}"
        for rel, content in test_files[:20]  # cap at 20 files to limit prompt size
    )

This gives the model concrete evidence (use statements, extern crate declarations, attribute
macros) to make accurate structural inferences without inflating the prompt significantly.

---

## Bug: LLMDependencyGraph truncates manifests to 4000 chars

### Location

`pipeline.py`, `LLMDependencyGraph._collect_manifests`, line 982.

### Context

`LLMDependencyGraph` is the fallback dependency graph implementation for non-Rust projects.
It reads all manifest files (Cargo.toml, package.json, pyproject.toml, etc.) and passes
their content to the model to extract the dependency graph.

At line 982:

    results.append((str(rel), content[:4000]))

Each manifest is truncated to 4000 chars before being passed to the model.

### The problem

A large workspace Cargo.toml can exceed 4000 chars when it lists many members, patches,
or workspace-level dependencies. The truncated manifest omits the bottom portion, which
may include the dependency declarations that establish cross-crate ordering. The model
produces a topological order that omits those crates or orders them incorrectly.

An incorrect topological order causes the pipeline to process modules in the wrong sequence.
A module that imports from another module is processed before that module exists, TASK 2
generates code that references missing types, and TASK 3 fails immediately with "cannot
find type X".

### Fix

Remove the char truncation at line 982. Manifest files are configuration, not source code -
a large Cargo.toml is 200-500 lines, well within any model context. If total manifest size
across a large monorepo would overflow the model context, apply a count limit on manifests
(keep the N deepest/closest to the target module) rather than silently cutting each one.

---

## Bug: TASK 0c pre-clean regex destroys original handwritten test blocks

### Location

`pipeline.py`, `task0c_augment_module`, line 4117.

### Context

Before the augmentation attempt loop, TASK 0c runs a "pre-clean" step that removes any
`gen_tests` blocks left over from a previous interrupted run. Without this, a prior run's
broken generated tests would cause a compile failure before the current run has a chance
to produce valid tests.

The pre-clean regex at line 4117 is:

    _gen_block_re = re.compile(r'\n+#\[cfg\(test\)\]\nmod \w+\s*\{.*', re.DOTALL)

### The problem

`mod \w+` matches ANY module name - including `mod tests`, the original handwritten test
block that ships with the crate. The pre-clean strips every `#[cfg(test)] mod ...` block
from every source file in the module, including the original tests.

This has a cascading effect:

1. `_extract_existing_tests` (line 3954) is explicitly designed to find `mod tests` and show
   it to the model as "how to correctly construct types used in this module." After the
   pre-clean, `mod tests` no longer exists. `_extract_existing_tests` returns "". The model
   receives no example of correct type construction.

2. The handwritten tests covered some lines before TASK 0c ran. Stripping them drops coverage
   below the baseline that `_measure_baseline_coverage` recorded. The augmentation loop
   spends its first attempt recovering coverage that was already there.

3. If `_extract_public_symbols` (line 3974) already returns only names without signatures,
   the model's only source of correct type usage was the existing test block. Destroying it
   leaves the model with no grounding at all.

### Fix

Narrow the pre-clean regex to match only pipeline-generated blocks, which always use a
`gen_` prefix by convention:

    _gen_block_re = re.compile(r'\n+#\[cfg\(test\)\]\nmod gen_\w+\s*\{.*', re.DOTALL)

This preserves `mod tests` while still removing `mod gen_tests`, `mod gen_tests_2`, and any
other pipeline-generated variants. The comment at line 4113-4116 already describes the
intent as "remove blocks left by a previous interrupted run" - the fix makes the regex
match the stated intent.

---

## Bug: _extract_public_symbols returns only names, no signatures or field definitions

### Location

`pipeline.py`, `_extract_public_symbols`, lines 3974-3982.

### Context

`_extract_public_symbols` is called at line 4196 during Phase C of TASK 0c to ground the
model with knowledge of what types and functions exist. The returned string is included in
the prompt with the instruction "ONLY use these - do NOT invent types or methods not listed."

The current implementation:

    for pat in [
        r'pub\s+(?:struct|enum|trait|type)\s+(\w+)',
        r'pub\s+(?:fn|const|static)\s+(\w+)',
    ]:
        names.extend(re.findall(pat, content))
    return ", ".join(sorted(set(names)))

### The problem

The function returns only a comma-separated list of names: `Config, Parser, ParseError`.

The model knows these names exist but cannot construct them. To write a test for a function
that takes a `Config`, the model must know Config's fields and their types. Without this:
- The model invents field names and types, producing code that does not compile.
- The compile fix loop (Phase A) burns an attempt repairing a structurally wrong constructor.
- With MAX_RETRIES=3, this often consumes the entire budget.

Struct fields, enum variants, and function parameter types are the critical missing information.
They are available in the source file content and can be extracted with slightly more complex
regex patterns.

### Fix

Extend the extraction to include struct field lists, enum variant names, and function
signatures. For structs:

    r'pub\s+struct\s+(\w+)\s*\{([^}]*)\}'

For each match, extract field names and types from the body. For enums:

    r'pub\s+enum\s+(\w+)\s*\{([^}]*)\}'

For each match, extract variant names. For functions, capture the full signature including
parameters:

    r'pub\s+fn\s+(\w+\s*\([^)]*\)\s*(?:->\s*[^\{]+)?)'

Format the result as structured lines rather than a flat list, so the model can use it as a
quick reference without reading the full source.

---

## Bug: _extract_existing_tests caps example tests at 2000 chars from the start

### Location

`pipeline.py`, `_extract_existing_tests`, line 3969.

### Context

`_extract_existing_tests` locates the `mod tests` block in a source file and returns it as
an example to guide the model. At line 3969:

    snippet = source_text[idx:idx + 2000]  # cap at 2000 chars to save context

### The problem

The cap is applied from the START of the test block. A test block longer than 2000 chars
is cut mid-function, delivering a fragment that:
- Ends in the middle of a function body, producing a syntactically incomplete Rust snippet
- Shows the model only the first few tests, which may use simpler type constructions than
  the later tests
- May cut off the only test that demonstrates how to construct a complex type the model
  needs to produce tests for

The model is told "here are existing tests as a reference for correct type construction"
and receives a broken, incomplete example. It may attempt to copy the truncated pattern,
producing compile errors in its generated code.

### Fix

Apply the 2000-char budget more intelligently. Rather than taking the first 2000 chars,
extract all complete test functions from the block and include as many as fit. A complete
test function starts at `#[test]` and ends at the matching closing brace. Prefer to include
the last N complete functions (which tend to cover more complex cases) rather than the first N.

---

## Bug: TASK 0c pre-flight prompt truncates compile error to 800 chars from the start

### Location

`pipeline.py`, `task0c_augment_module`, line 4076.

### Context

The pre-flight stage fixes compile errors in existing test code before the augmentation loop
begins. For each broken file, it builds a prompt that includes the compile error. At line 4076:

    {compile_err[:800]}

### The problem

Rust compiler errors begin with high-level context (crate name, edition, file being compiled)
and the specific error appears lower, often beyond 800 chars. A cascading error in a file with
multiple issues will list all errors sequentially. The first 800 chars typically contain the
first error but miss all subsequent errors.

The model sees only the first error, fixes it, and returns a file that still has the remaining
errors. The outer loop retries but burns another attempt on the same truncated view. After
MAX_RETRIES=3 pre-flight attempts, the function returns 0.0.

### Fix

Increase the truncation limit to 4000 chars. Compiler error output is primarily ASCII and
compresses well in context. Alternatively, parse the error output to extract error codes and
file positions, then show the model a structured summary rather than raw truncated text:

    error[E0432]: unresolved import at line 12
    error[E0277]: trait `Display` not implemented for `Foo` at line 34

This structured form is shorter, preserves all errors, and gives the model cleaner signal.

---

## Bug: TASK 0c pre-flight prompt truncates file content to 6000 chars from the start

### Location

`pipeline.py`, `task0c_augment_module`, line 4080.

### Context

The pre-flight fix prompt shows the model the current content of the broken file. At line 4080:

    {current_content[:6000]}

### The problem

The truncation is applied from the start of the file, not the error location. A 6000-char
cutoff is roughly 200-250 lines. The `#[cfg(test)]` block that contains the broken test code
is typically appended at the END of the file (that is how TASK 0c wrote it). For a source
file with 300+ lines of implementation, the test block lives entirely after the 6000-char
cutoff and is invisible to the model.

The model is asked to "fix the test code so it compiles" but is not shown the test code.
It produces a response that either changes nothing visible in the prompt or modifies
implementation code it can see, leaving the broken test block untouched. The fix attempt
fails, and the attempt is burned.

### Fix

Instead of truncating from the start, locate the compile error position in the file and
center the context window around it. If the error mentions a specific line number, show
`max(0, error_line - 50)` through `error_line + 100` of the file. If the error mentions
the `#[cfg(test)]` block (by referencing a test function name), show the full `#[cfg(test)]`
section regardless of position. Fall back to `smart_truncate` (keep head and tail) only if
no position is determinable.

---

## Bug: load_original_structure has prefix collision when module names share a prefix

### Location

`pipeline.py`, `load_original_structure`, lines 4549-4566.

### Context

`load_original_structure` is called by `StaticTask2Generator` at line 3128 to retrieve the
pre-strip file list for a module. It searches the `.original_structure/` directory for a
matching `.txt` file.

### The problem

The primary match at lines 4557-4560 is:

    content = candidate.read_text()
    paths = [l.strip() for l in content.splitlines() if l.strip()]
    if paths and paths[0].startswith(module_name.replace("-", "_")):
        return paths

`paths[0]` is the first entry in the saved file list, which is a relative path like
`argh_shared/src/lib.rs`. The check is `paths[0].startswith(module_name)`.

For a workspace with both `argh` and `argh_shared`, when `module_name = "argh"`:
- The file `argh_shared.txt` has `paths[0] = "argh_shared/src/lib.rs"`
- `"argh_shared/src/lib.rs".startswith("argh")` is True
- If `argh_shared.txt` appears before `argh.txt` in filesystem iteration order (which is
  arbitrary on ext4 and similar filesystems), the function returns the argh_shared structure
  when called for argh

TASK 2 then generates files at `argh_shared/src/lib.rs` paths when working on the `argh`
module, writing to the wrong module directory. The argh module ends up with no generated
files (none match its actual directory), and TASK 3 fails with "no source files found."

Additionally, lines 4554-4556 contain dead code: `stem` is computed but never used, and the
`if module_name in ...: pass` block reads the file and immediately discards the result.

### Fix

Match against the candidate filename (the `.txt` file stem), not the content. The filename
is the `module_rel` saved at `_save_original_structure` line 4536:

    for candidate in out_dir.glob("*.txt"):
        stem = candidate.stem  # e.g. "argh_shared" for argh_shared.txt
        if stem == module_name or stem.replace("-", "_") == module_name.replace("-", "_"):
            content = candidate.read_text()
            return [l.strip() for l in content.splitlines() if l.strip()]
    return []

This is unambiguous, never collides on prefix-sharing names, and eliminates the dead code
and redundant file reads. The fallback loop at lines 4562-4566 can be removed entirely
since the stem comparison is already exact.

---

## Bug: TASK 2 never receives test files -- model generates blind against requirements alone

### Location

`pipeline.py`, `StaticTask2Generator.generate`, lines 2913-2929.
`pipeline.py`, `LLMTask2Generator.generate`, lines 3010-3093.
`pipeline.py`, `StaticFileListTask2Generator.generate`, lines 3207-3218.

### Context

All three TASK 2 generators construct a prompt containing: genesis.md, functional_requirements.md,
project_profile, workspace_structure, locked_dep_context, and (optionally) whole_project_context
with test files for zero-test modules. None of them pass the test files for the current module
to a standard reconstruction call.

### The problem

The model generates source code from functional requirements alone. The requirements document
is produced by TASK 1, which extracts from the test files. If TASK 1 extracted incomplete or
ambiguous requirements, the model generating in TASK 2 has no way to detect this. It cannot
cross-reference its output against the actual tests. It produces code that satisfies the written
requirements but fails tests that exposed a requirement TASK 1 missed.

TASK 3 then fails. TASK 4 updates genesis.md. The next iteration runs TASK 1 again (which may
or may not improve), then TASK 2 again with the same blindness. The cycle repeats for 50+ iterations
(as seen in the failure log) because the root cause -- TASK 2 cannot see the tests -- is never
addressed.

Including the test files in TASK 2 has two benefits:
- The model can check its own output against concrete test calls and assertions.
- When the requirements are ambiguous, the model resolves the ambiguity from the tests directly
  rather than guessing.

### Fix

Pass `module_test_files` (the current module's test files, already available at the call site
in main) to all three generators. Add a `test_files` parameter to each generator's `generate`
method signature and format the test files as a `<TESTFILE>` block in the prompt:

    --- Test files (read-only -- use these to verify your implementation matches what is tested) ---
    <TESTFILE path="argh/tests/basic.rs">
    ...content...
    </TESTFILE>

This does not replace the functional requirements -- the requirements remain the primary
specification. The test files serve as ground truth for disambiguation and self-verification.
Pass `module_test_files` if it has content; fall back to `test_files` (all project tests)
if the module has zero own tests (consistent with how TASK 1 handles this case at line 5001).

---

## Bug: StaticFileListTask2Generator truncates sibling files to 1500 chars when building cross-file context

### Location

`pipeline.py`, `StaticFileListTask2Generator.generate`, line 3168.

### Context

When generating file N in a sequence, the generator passes files 1 through N-1 as context
so the model can see what types and functions have already been defined. At line 3168:

    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content[:1500]}\n</FILE>')

### The problem

1500 chars is approximately 50 lines of Rust source. A typical source file (lib.rs for a
moderately complex crate) is 200-600 lines. The model sees only the import block and the
first few type definitions of each sibling file. Struct fields, enum variants, function
signatures, and `impl` blocks that appear later in the file are invisible.

When the model generates file N (e.g. `parser.rs`) and needs to use a type from file N-2
(e.g. `config.rs`), it sees only the first 50 lines of `config.rs`. If `Config`'s fields
are defined after line 50, the model invents field names and types. The resulting `parser.rs`
uses `config.field_a` when the actual field is `config.timeout`. The build check fails, the
fix loop tries to repair it without knowing what the fields actually are, and the attempt is
wasted.

### Fix

Remove the `[:1500]` truncation. Pass each sibling file in full:

    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')

No artificial cap. If the model's context window is exceeded the API will return an error,
which is an observable and diagnosable failure. Silent truncation of type context is worse
than an explicit failure because it produces wrong code that looks plausible.

---

## Bug: Chunk cache ignores genesis.md changes -- stale partials served after every TASK 4 update

### Location

`pipeline.py`, `StaticTask1Synthesizer._chunked`, lines 2285-2320.
`pipeline.py`, `_chunk_hash`, line 1299.

### Context

TASK 1 is called on every iteration. For large projects (test files exceeding 20,000 chars),
it splits test files into chunks and issues one LLM call per chunk. The results are cached on
disk, keyed by the hash of the chunk content:

    h = _chunk_hash(tests_section)  # sha256 of test text only

On a cache hit, the cached partial is loaded without calling the model:

    if partial_file.exists():
        ...load and reuse...
        continue
    cached = _load_cached_chunk(cache_dir, h)
    if cached:
        ...reuse...

### The problem

The cache key is `sha256(tests_section)` -- a hash of the test file content only. Test files
do not change between iterations (they are the preserved test suite). Therefore the same hash
appears in iteration 1, iteration 2, iteration 50, and every iteration after.

TASK 4 updates genesis.md after each failure. The updated methodology contains lessons like
"always capture struct field types" or "always extract error variant names." These lessons should
guide the next TASK 1's chunk extraction -- the whole point of updating genesis.md. But the chunk
prompt does not include genesis.md (Blinker 4, already documented), and even if it did, the
cached partials from iteration 1 would be served unchanged. The model never applies the updated
methodology to the raw test chunks. All TASK 4 improvements to genesis.md are silently discarded
at the cache layer.

Every iteration runs: genesis updates (TASK 4) -> TASK 1 reads genesis in synthesis prompt ->
synthesis merges cached stale partials -> genesis guidance has zero effect on what information
was captured from the tests.

### Fix

Include the genesis.md hash in the cache key:

    import hashlib
    genesis_hash = hashlib.sha256(existing_genesis.encode()).hexdigest()[:8]
    combined_hash = _chunk_hash(tests_section + genesis_hash)

This invalidates all cached partials whenever genesis.md changes. The first run after a TASK 4
update re-extracts all chunks under the new methodology. Subsequent runs within the same genesis
version reuse the cache normally.

The partial files on disk (`.task1_partials/chunk_N.txt`) should also be invalidated, not just
the content-addressed cache. The simplest approach: clear `.task1_partials/` at the start of each
TASK 1 run when the genesis hash has changed from the last run (track the genesis hash in a small
sentinel file alongside the partials directory).

---

## Bug: TASK 1 Stage-1 group synthesis has no genesis.md and truncates when groups are large

### Location

`pipeline.py`, `StaticTask1Synthesizer._synthesize_group`, lines 2228-2263.
`pipeline.py`, `LLMTask1Synthesizer._synthesize_group`, lines 2580-2618.

### Context

In the chunked path, TASK 1 merges chunk partials in two stages:
- Stage 1: merge SYNTHESIS_GROUP_SIZE=10 partials per group call.
- Stage 2: merge all group summaries into the final requirements document.

Genesis.md is included in the Stage-2 final synthesis prompt (lines 2415, 2522) but NOT in the
Stage-1 group merge prompt. The Stage-1 prompt at line 2241 (Static) and 2592 (LLM) has no
genesis section at all.

Additionally, when 10 partials are combined into `combined`, if the combined length exceeds
INTERMEDIATE_CHAR_LIMIT * 2 = 32,000 chars, the middle is dropped:

    if len(combined) > INTERMEDIATE_CHAR_LIMIT * 2:
        combined = smart_truncate(combined, total_limit=INTERMEDIATE_CHAR_LIMIT * 2,
                                  head=INTERMEDIATE_CHAR_LIMIT // 2,
                                  tail=INTERMEDIATE_CHAR_LIMIT * 3 // 2)

### The problems

Problem 1 -- no genesis in Stage-1: The methodology that genesis.md encodes (e.g. "always
capture struct fields with their types") should guide every extraction step. Stage 1 is the
first synthesis pass, where specific signal is most at risk of being lost through condensation.
Without genesis, the model condenses 10 partials into a summary using its default heuristics,
which may differ from what genesis prescribes. The Stage-2 genesis-guided synthesis then works
with already-condensed, already-lossy intermediates.

Problem 2 -- partial truncation in Stage-1: When 10 chunk partials exceed 32,000 chars combined,
the middle partials (chunks 3 through 7 in a 10-chunk group) are silently dropped from the
merge call. The model produces a "group summary" that actually covers only 60% of the chunks
it was supposed to merge. The dropped chunks' requirements are permanently lost -- they never
appear in any Stage-2 input.

### Fix

Problem 1: Pass genesis.md to `_synthesize_group`. Add a `genesis` parameter to the method
signature and include a genesis section in the prompt:

    --- genesis.md (methodology -- use to guide which information to preserve in this merge) ---
    {genesis}

Problem 2: Remove the `smart_truncate` on the combined partials. Instead, when 10 partials
would exceed the context budget, reduce the group size dynamically: if combined length > limit,
split the group further and emit multiple smaller intermediates rather than truncating. A group
of 10 that overflows becomes two groups of 5, each merged independently.

---

## Bug: TASK 4 truncates functional_requirements.md from the middle

### Location

`pipeline.py`, `task4`, line 3604-3605.

### Context

TASK 4 receives functional_requirements.md to analyze which requirements were incorrectly
implemented. At lines 3604-3605:

    functional_req = smart_truncate(read(functional_req_path), total_limit=TASK4_DOC_LIMIT,
                                    head=TASK4_DOC_LIMIT // 4, tail=3 * TASK4_DOC_LIMIT // 4)

`TASK4_DOC_LIMIT = 20,000`. The head is 5,000 chars (25%) and the tail is 15,000 chars (75%).

### The problem

functional_requirements.md is structured sequentially: module paths, then type definitions,
then function signatures, then invariants. For a complex project with many types, the middle
section (type definitions and key function signatures) is exactly what gets dropped.

TASK 4 must identify "which requirement gap caused this test failure." If the type definition
that was incorrectly implemented is in the dropped middle, TASK 4 cannot find the gap. It
analyzes the visible head (introductory text, high-level structure) and the visible tail
(later invariants) and produces a genesis update that addresses neither the actual failure
nor the actual gap.

The same truncation applies to genesis.md (line 3602-3603) and is documented as a separate
entry above. Both need the same fix.

### Fix

Do not truncate functional_requirements.md by position. Pass it in full. A functional
requirements document for a complex module is 5,000-20,000 chars -- within any modern LLM's
context window. If it genuinely exceeds the context window, apply a relevance-first truncation:
scan the test output for specific function names and type names, find the sections of the
requirements document that mention them, and include those sections in full rather than keeping
an arbitrary head/tail split.

---

## Bug: TASK 4 truncates test output aggressively, losing specific failure detail

### Location

`pipeline.py`, `task4`, lines 3606, 3623.

### Context

TASK 4 receives the current test output and (when available) the previous iteration's test
output. Both are truncated:

    truncated_output = smart_truncate(test_output)       # defaults: 8000 total, 2000 head, 6000 tail
    prev_truncated = smart_truncate(_prev_output, total_limit=4000, head=500, tail=3500)

### The problem

Test output from `cargo test` for a moderately large workspace with multiple failures can be
40,000-100,000 chars. The 8,000 char cap drops most of the specific error messages and assertion
failures that TASK 4 needs to identify which exact behavior is failing.

The tail-bias (6,000 of 8,000 chars from the end) is based on the assumption that summaries
appear at the end of test output. This is correct for cargo test ("test result: FAILED. 3 passed;
7 failed"). But the specific failure messages -- the assertion text, the panic location, the
actual vs. expected values -- appear in the middle: one block per failing test. If there are 7
failing tests and each block is 1,000 chars, the middle 5 are dropped.

TASK 4 sees only the last 2 failing tests' detail plus the summary line, and updates genesis.md
to fix those 2 cases. The other 5 failures are invisible and remain unfixed in the next iteration.

The previous iteration's output is truncated even more aggressively (4,000 chars) making
regression analysis across iterations nearly useless for anything beyond the pass count delta.

### Fix

Remove positional truncation from test output. Parse the test output into discrete sections:
one section per failing test (delimited by "--- FAILED" / "thread panicked" markers). Pass all
failing test sections in full. If the total would exceed the context budget, apply a count limit
(show the first 20 failing tests, not a random middle-truncated slice).

For previous iteration output, the full output is less important than the list of which tests
passed/failed. Extract the test names that changed state (passed in previous, failed now or vice
versa) and pass only those, not the full output.

---

## Bug: Failure log truncated to 8000 chars from the tail -- early iterations invisible to TASK 4

### Location

`pipeline.py`, `load_failure_log`, lines 1344-1351.

### Context

The failure log (`failure_log.md`) records every iteration's outcome: failure type, test pass
count, and summary. It is fed into TASK 4 so the model can "avoid repeating failed approaches."
At lines 1349-1350:

    if len(content) > max_chars:
        content = "[... earlier entries omitted ...]\n\n" + content[-max_chars:]

`max_chars = 8000`. Each log entry is roughly 100-150 chars. 8,000 chars holds approximately
60-80 recent entries.

### The problem

The failure log for the argh project shows 56 consecutive `no_tests` failures for `argh_shared`
across multiple iterations. TASK 4 receives only the last 60-80 entries. If the pipeline has run
100+ iterations (which the current failure log suggests), TASK 4 never sees entries from iterations
1-40. It cannot know:
- That approach X was tried 80 iterations ago and failed
- That the pipeline has been stuck on `no_tests` for a very long time
- What specific changes to genesis.md were made in earlier iterations and what their effects were

Without this history, TASK 4 repeatedly makes the same type of genesis update (add a new rule
about structural information), not knowing it has already added and reversed that rule multiple
times. The lack of history is one reason the pipeline loops indefinitely on the same module.

### Fix

Do not truncate the failure log by tail. The failure log is deliberately compact (100-150 chars
per entry). 100 iterations produce 10,000-15,000 chars, which fits within any model context. If
the log genuinely grows too large (1,000+ iterations), apply a structured summary: keep the full
last 30 entries (detailed recent history) and include a statistical summary of earlier entries
(e.g. "iterations 1-70: 68 build failures, 2 test failures, no improvement").

---

## Bug: task2_inner_loop passes truncated build error to the next TASK 2 regeneration

### Location

`pipeline.py`, `task2_inner_loop`, line 3378.

### Context

When the generate_fix_loop fails to repair the generated code after its fix attempts, `task2_inner_loop`
passes the final build error back to TASK 2 as `build_error` for the next full regeneration attempt.
At line 3378:

    build_error = smart_truncate(build_output, total_limit=6000, head=3000, tail=3000)

### The problem

`cargo check --workspace` output for a workspace with multiple compilation errors across
multiple crates can produce 30,000-80,000 chars. 6,000 chars covers the first 3,000 (which is
often just the cargo header and the first error) and the last 3,000 (often the "aborting due to N
errors" summary). The specific per-function, per-file error messages in the middle are dropped.

TASK 2 receives an instruction "fix ALL errors below" with only a fraction of the errors shown.
It fixes the 2-3 errors it can see and regenerates. The next build check still fails because the
errors in the dropped middle were never addressed. This wastes a full TASK 2 regeneration attempt.

### Fix

Pass the full build output to TASK 2. For a workspace with many errors, parse the output into
per-file error blocks and show all of them. If the total genuinely exceeds context limits, show
the full error output for the specific files that will be regenerated, not an arbitrary
head+tail of the full output.

---

## Bug: LLMWorkspaceScanner also truncates each manifest to 4000 chars

### Location

`pipeline.py`, `LLMWorkspaceScanner._collect_manifests`, line 626.

### Context

`LLMWorkspaceScanner` is the language-agnostic workspace scanner (default for non-Rust projects).
It collects all manifest files and passes them to the model to produce the workspace structure
description. At line 626:

    results.append((str(rel), content[:4000]))

### The problem

This is the same truncation bug already documented for `LLMDependencyGraph._collect_manifests`
(line 982). Both classes use the same `_MANIFEST_NAMES` set and the same 4000-char truncation.
Both produce incorrect workspace descriptions when manifests exceed 4000 chars. The two classes
are separate implementations with duplicated logic -- fixing one without fixing the other leaves
the workspace scanner broken even after the dependency graph is fixed.

### Fix

Remove the `[:4000]` truncation in both `LLMWorkspaceScanner._collect_manifests` (line 626)
and `LLMDependencyGraph._collect_manifests` (line 982). The fix is identical in both locations.
Consider extracting `_collect_manifests` into a shared static method to avoid future divergence.

---

## Bug: proc-macro TASK 1 truncates test files from the start at 24,000 chars

### Location

`pipeline.py`, `_task1_proc_macro`, lines 2654-2655.

### Context

`_task1_proc_macro` handles TASK 1 for proc-macro crates. After filtering to files with
`#[derive(...)]` or `#[argh(...)]` patterns, it formats the relevant test files and then
applies a hard truncation at line 2654:

    if len(tests_section) > 24_000:
        tests_section = tests_section[:24_000] + "\n...[truncated]"

### The problem

The truncation is applied from the START of the formatted test section, not from a semantic
boundary. If the relevant test files (those using derive macros) total more than 24,000 chars,
the last portion -- which may contain the most complex derive usage patterns, edge-case attributes,
and error-handling tests -- is simply cut off.

Proc-macro crates' requirements are entirely derived from derive-attribute usage in tests. Missing
any test that uses `#[argh(option)]`, `#[argh(description = "...")]`, or a complex field type
means the proc-macro implementation does not handle that attribute. The generated proc-macro
silently misses an attribute variant, tests fail, and TASK 4 updates genesis.md without knowing
what the proc-macro was supposed to parse.

### Fix

Remove the `[:24_000]` truncation. Pass all relevant test files in full. If the model's context
window is genuinely too small, split proc-macro TASK 1 into the same chunked+hierarchical path
used by the regular TASK 1 -- it already handles large test file sets correctly.

---

## Root cause (architectural): all context limits calibrated for 8192-token Ollama -- replace with model-aware auto-detection

### Note

The individual bug entries above each document a specific truncation or missing-context problem
at a precise line number. This entry explains why they all share the same root cause and how a
single architectural change can fix all of them at once. The implementing agent should apply
both: fix each call site as described in the individual entries, AND apply the architectural
change below so that future call sites do not silently revert to the old assumption.

### The root cause

Every hard limit in pipeline.py was set when the pipeline ran on Ollama with `num_ctx=8192`.
The code comments at lines 69-75 make this explicit:

    # ~4 chars per token. With num_ctx=8192 and prompt overhead of ~1k tokens,
    # the safe content budget is ~7000 tokens = ~28k chars per call.
    SINGLE_CALL_CHAR_LIMIT = 20_000
    # Max chars per chunk when splitting test files across multiple calls.
    # Sized to fit in num_ctx=8192 with room for prompt and response.
    CHUNK_CHAR_LIMIT = 20_000

The pipeline now runs on Claude with a 200,000-token context window (~800,000 chars). Every
limit derived from the 8192-token assumption is now roughly 40x too small. The model is
receiving fragments of its inputs as if it were a pocket calculator, when it can process a
large novel in one call.

The `autopilot.md` startup command `OLLAMA_CONTEXT_LENGTH=8192 nohup python pipeline.py ...`
preserves this constraint explicitly, still capping the context to 8192 tokens even on Claude.

This is not a set of individual bugs. It is one architectural assumption -- "context window is
8192 tokens" -- that propagates into every truncation decision in the pipeline. Fixing them
individually would require touching ~20 call sites, and any new call site would silently revert
to the old assumption. The fix must be architectural.

### Affected constants and call sites

The following constants and hardcoded limits are all direct descendants of the 8192-token assumption:

| Location | Current value | Origin |
|---|---|---|
| `SINGLE_CALL_CHAR_LIMIT` (line 71) | 20,000 chars | "num_ctx=8192" comment |
| `CHUNK_CHAR_LIMIT` (line 75) | 20,000 chars | "num_ctx=8192" comment |
| `INTERMEDIATE_CHAR_LIMIT` (line 95) | 16,000 chars | derived from same |
| `TASK4_DOC_LIMIT` (line 120) | 20,000 chars | derived from same |
| `generate_fix_loop` per-file cap (line 3447) | 2,000 chars | "context budget" |
| `generate_fix_loop` total source cap (line 3450) | 12,000 chars | "context budget" |
| `generate_fix_loop` error cap (line 3458) | 2,000 chars | "context budget" |
| `task2_fix_loop` req cap (line 3514) | 2,000 chars | "context budget" |
| `task2_inner_loop` build error (line 3378) | 6,000 chars | smart_truncate default |
| `load_failure_log` max_chars (line 1344) | 8,000 chars | hardcoded |
| `_extract_existing_tests` cap (line 3969) | 2,000 chars | "save context" comment |
| `LLMWorkspaceScanner._collect_manifests` (line 626) | 4,000 chars per file | hardcoded |
| `LLMDependencyGraph._collect_manifests` (line 982) | 4,000 chars per file | hardcoded |
| `_task1_proc_macro` test cap (line 2654) | 24,000 chars | hardcoded |
| `task0c_augment_module` compile error (line 4076) | 800 chars | hardcoded |
| `task0c_augment_module` file content (line 4080) | 6,000 chars | hardcoded |
| `_extract_existing_tests` snippet (line 3969) | 2,000 chars | hardcoded |

### Fix

Add a `_model_context_chars()` function at startup that reads the actual context window from
the environment and model name, then derive all limits from it:

    def _model_context_chars() -> int:
        ollama_ctx = os.getenv("OLLAMA_CONTEXT_LENGTH")
        if ollama_ctx:
            return int(ollama_ctx) * 4
        if MODEL.startswith("claude"):
            return 200_000 * 4  # Claude 3+ context window
        return 8_192 * 4  # safe fallback

    MODEL_CONTEXT_CHARS = _model_context_chars()

Replace the constants with fractions of `MODEL_CONTEXT_CHARS`:

    SINGLE_CALL_CHAR_LIMIT = MODEL_CONTEXT_CHARS // 4
    CHUNK_CHAR_LIMIT       = MODEL_CONTEXT_CHARS // 4
    INTERMEDIATE_CHAR_LIMIT = MODEL_CONTEXT_CHARS // 5
    TASK4_DOC_LIMIT        = MODEL_CONTEXT_CHARS // 4

For the hardcoded per-site limits that are not driven by constants, replace them with no cap
(pass content in full) or with `MODEL_CONTEXT_CHARS`-relative expressions. The principle is:
no artificial content limit should exist that is smaller than what the model can actually process.
If a call would genuinely exceed the context window, the API returns an observable error --
which is diagnosable and fixable. Silent truncation of type definitions, error messages, and
test files produces wrong code that looks plausible and is much harder to debug.

The `OLLAMA_CONTEXT_LENGTH=8192` prefix in `autopilot.md` must also be removed from the
startup command so that Claude runs without an artificial constraint. The variable was a
workaround for Ollama's hard limit and has no purpose when running on Claude.

---

## Architecture: introduce ARCHITECTURE.md with step payload schemas and anti-corruption validation

### The problem

The pipeline advances from step to step regardless of output quality. There is no formal
definition of what each task must produce, and no check that it actually produced it before
the next task starts. Garbage propagates silently:

- TASK 1 produces a vague or near-empty `functional_requirements_{module}.md`
- TASK 2 generates weak or empty code against it
- TASK 3 fails (or reports 0 tests ran)
- TASK 4 updates genesis.md without knowing which step failed
- The cycle repeats 50+ times (as seen in failure_log.md for argh_shared)

At no point does the pipeline say "the output of TASK 1 does not meet the minimum bar to
proceed -- retry TASK 1 before running TASK 2." Every step operates on implicit assumptions
about what it will receive, and those assumptions are never written down or enforced.

The 56-iteration no_tests loop in the failure log is a direct consequence: TASK 1 was
producing output that did not contain enough concrete information for TASK 2 to generate
testable code, but the pipeline kept advancing anyway because there was no boundary check.

### What ARCHITECTURE.md should contain

A new file `ARCHITECTURE.md` at the pipeline root defines the hard contracts between steps.
It is the pipeline equivalent of what `functional_requirements_{module}.md` is to the target
project: a specification of what each component must produce, not how it produces it.

Structure:

    1. Data flow: which files flow between which tasks and in what direction
    2. Per-task input contract: what files must exist, in what format, before the task runs
    3. Per-task output contract: what files must be created, minimum content requirements
    4. Validation rules: programmatic checks that enforce each contract
    5. Retry policy: what happens when validation fails (retry same task vs. escalate)

### Step payload schemas

#### TASK 0a output: project_profile.md

Qualitative requirements:
- Identifies the language and build system
- Lists every crate/module with its type (lib, bin, proc-macro) and directory path
- States the minimum set of files required for each crate to compile

Quantitative requirements:
- Length >= 300 chars
- At least one module entry with a path that exists on disk
- Build system identifier matches a file on disk (e.g. "Cargo" requires Cargo.toml to exist)

Anti-corruption check before TASK 1:
- Every module path listed in the profile exists as a directory on disk
- No module is listed with a type that contradicts the manifest (e.g. profile says "bin" but
  Cargo.toml says "lib = ...")
- If any check fails: re-run TASK 0a, do not proceed to TASK 1

#### TASK 0b output: dependency order

Qualitative requirements:
- A total ordering of modules where each module appears after all its dependencies
- No circular dependencies

Quantitative requirements:
- At least one module in the list
- Every module listed appears in project_profile.md
- No module appears twice

Anti-corruption check before the module loop:
- Topological order is valid (no module precedes a module it depends on per the manifests)
- If invalid: re-run TASK 0b, do not start the module loop

#### TASK 0c output: augmented source files

Qualitative requirements:
- Coverage of the target module >= configured threshold (default 80%)
- All appended test blocks compile (cargo test --no-run passes)
- Appended blocks use only types and functions that exist in the module

Quantitative requirements:
- Coverage number is strictly higher than before augmentation
- gen_tests blocks have matching braces (syntactically complete)
- At least one new test function was added

Anti-corruption check before stripping:
- Final coverage measurement >= threshold
- `cargo test -p {module} --no-run` exits 0
- If coverage did not increase from baseline: log a warning and continue (augmentation is
  best-effort), but do NOT advance if compile check fails

#### TASK 1 output: functional_requirements_{module}.md

This is the most critical boundary. The entire reconstruction depends on the quality of this
document. It must be validated rigorously before TASK 2 runs.

Qualitative requirements:
- Contains at least one concrete type definition: a struct with named fields and their types,
  or an enum with named variants
- Contains at least one function signature with parameter names, parameter types, and return type
- Every type name in the document appears literally in at least one test file for this module
- The document is not generic methodology text -- it must contain identifiers specific to this
  module (type names, function names, constant names that appear in the tests)
- The document does not describe the pipeline itself (a failure mode seen in practice where the
  model writes about "functional requirements" as a concept rather than the module's requirements)

Quantitative requirements:
- Length >= 500 chars
- At least 3 distinct type or function names that appear in the module's test files
- At least one section with a field-level or parameter-level type specification
  (e.g. "field `timeout: Duration`" not just "has a timeout field")

Anti-corruption check before TASK 2:
- Extract all identifiers (CamelCase words and snake_case words) from the requirements doc
- Intersect with identifiers from the test files
- If intersection has fewer than 3 elements: requirements are too generic, retry TASK 1
- If length < 500 chars: requirements are too sparse, retry TASK 1
- If the document contains phrases like "functional requirements are" or "this document
  describes": it is meta-commentary not specification, retry TASK 1
- Maximum 2 retries before escalating to TASK 4 with failure_type="requirements_failure"

#### TASK 2 output: generated source files

Qualitative requirements:
- All file paths listed in the original structure for this module are present on disk
- Each generated file is non-empty and contains actual Rust (or target language) code
- The public API exported by the generated files matches what the test files import
  (every `use {module}::{Name}` in the test files resolves to a definition in the generated files)
- No file consists only of placeholder content (empty structs, unimplemented!() bodies on
  every function, or just comments)

Quantitative requirements:
- All expected file paths exist (0 missing files)
- Each file >= 50 chars
- At least one type definition per type named in functional_requirements_{module}.md
- `cargo check -p {module}` exits 0 (or at most 1 retry for a fix)

Anti-corruption check before TASK 3:
- All expected paths exist
- `cargo check -p {module}` passes (build check, not full test compile)
- For each type in functional_requirements_{module}.md: a `struct {Name}` or `enum {Name}`
  appears in the generated code
- If any file is missing or empty: do not run TASK 3, send back to TASK 2 with specific
  missing-file error
- If build check fails after 1 fix attempt: send back to TASK 2 with full error, not TASK 3

#### TASK 3 output: test results

Qualitative requirements:
- At least 1 test ran (0 tests is not a passing result, it is a configuration failure)
- Test output is parseable into per-test pass/fail records
- Failure messages include the test name, failure type, and (for assertion failures) the
  actual vs. expected values

Quantitative requirements:
- tests_run > 0: if 0 tests ran, this is failure_type="no_tests", not a valid test run
- Pass count is a non-negative integer
- Fail count is a non-negative integer
- Pass + fail = tests_run

Anti-corruption check before TASK 4:
- If tests_run == 0: do not proceed to TASK 4 with a "no_tests" failure in the normal loop.
  Instead, diagnose WHY 0 tests ran: does the module have any test files? Does the test
  binary compile? Is the test filter excluding everything? The diagnosis, not the raw "0 tests"
  result, is what TASK 4 needs to act on.
- If tests_run > 0 and all pass: mark module as locked, do not call TASK 4

#### TASK 4 output: updated genesis.md

Qualitative requirements:
- genesis.md grew (at least one new paragraph was added)
- The new content references at least one concrete identifier from the failing test output
  (a test function name, a type name, or a specific error message)
- The new content does not contradict an existing lesson in genesis.md
- The new content does not repeat verbatim a lesson already present

Quantitative requirements:
- genesis.md length after > genesis.md length before
- New content references at least one identifier that appears in the test output
- No section of new content is a substring of existing content (deduplication check)

Anti-corruption check before the next TASK 1:
- Verify genesis.md actually changed
- If genesis.md is identical to before: TASK 4 produced no update, which means it has no
  new insight to offer. Do not run another TASK 1 immediately -- this is a signal that the
  failure is structural (missing context, wrong task order) rather than a requirements gap.
  Log this as failure_type="genesis_stall" and surface it prominently.

### Implementation in pipeline.py

Add a `_validate_step_output(step_name, context)` function for each step. Each validator
returns `(ok: bool, reason: str)`. Call it at the end of each task function before returning.

If validation fails, the task function raises a `StepOutputInvalid(reason)` exception.
The caller (the main loop or the module loop) catches it and either retries the same task
or escalates, according to the retry policy in ARCHITECTURE.md.

The validators are not LLM calls. They are deterministic checks on file existence, length,
identifier intersection, and build exit codes. They run in milliseconds and catch bad outputs
before the pipeline wastes a full TASK 2 + TASK 3 + TASK 4 cycle on them.

### The no_tests loop as a concrete example

The argh_shared module has been stuck in a no_tests loop for 56+ iterations. With the
above contracts in place:

- TASK 1 anti-corruption: functional_requirements_argh_shared.md must have >= 3 identifiers
  from argh_shared test files. If argh_shared has zero test files, this check triggers
  immediately with "no test files found for this module" -- not after 56 iterations.
- TASK 3 anti-corruption: if 0 tests ran, diagnose before calling TASK 4. If the module
  has no test files, the diagnosis is "module has no tests -- cannot validate reconstruction."
  This surfaces in iteration 1, not iteration 56.
- TASK 4 anti-corruption: if genesis.md did not change, log genesis_stall and stop the loop.
  The pipeline cannot fix a no-test module by updating genesis.md.

The correct response to "module has no test files" is not 56 more TASK 4 updates to genesis.md.
It is a specific handling path: mark the module as "no_tests" in the locked state, generate
a minimal stub implementation, and move on. ARCHITECTURE.md defines this path explicitly so
the pipeline never needs to rediscover it through iteration.
