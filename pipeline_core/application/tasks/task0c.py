"""TASK 0c — per-module coverage augmentation engine (extracted verbatim).

The 1316-line body is moved VERBATIM from pipeline.py; the only addition is a prelude that
rebinds every collaborator as a local from the injected :class:`Task0cDeps`, so the body is
byte-for-byte unchanged (zero substitution risk). All regex was factored into named pipeline
helpers before the move (they are injected). subprocess/git I/O is injected (the module never
imports subprocess); `_current_cluster` is resolved at call time via a getter. No import of
pipeline.py.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...domain.behaviour_pinning import (
    behaviours_pinned_by,
    pinnable_specs,
    render_behaviour_list,
    select_behaviours_for_file,
)
from ...domain.behaviour_select import drop_capture_owned
from ...domain.cluster_plan import target_scope_rule
from ...domain.coverage import PerFileCoverage, coverage_pass_outcome
from ...domain.file_coverage_floor import FileFloor, UncoveredSplit, judge_files, lines_in
from ...domain.fn_execution import (
    FnCoverageRecord,
    FnTarget,
    FunctionExecution,
    aimed_fn_outcomes,
    flipped_cfg_set,
    inherited_attempts,
    note_attempt,
    note_verdicts,
    site_label,
    unambiguous_verdicts,
    withhold_pending,
)
from ...domain.generation import (
    as_integration_test,
    gen_tests_integration_rel,
    is_claimed_pipeline_gen_test_path,
    merge_gen_tests_blocks,
    parse_gen_tests_integration_filename,
)
from ...domain.llm_output import extract_code_blocks, strip_uncovered_markers
from ...domain.oracle_admission import (
    AdmissionPolicy,
    BlockAdmission,
    judge_block,
    judge_block_contract,
    weakening_reason,
)
from ...domain.oracle_visibility import declared_items, fn_line_spans, public_item_names
from ...domain.refine import Attempt, FixProgress, LoopState
from ...domain.rust_idents import camelcase_type_names, defined_type_names
from ...domain.text import strip_think_blocks
from ...domain.workspace_layout import STREAM_LOG_DIR
from ...log import emit, log
from ...prompts.sections import trait_dispatch_route
from ..fix_loop import FixOutcome, drive_fix_loop
from ..reconstruct.cargo_errors import error_type_signals
from .fn_criterion import (
    FnCriterionInputs,
    fold_reading,
    read_by_construction,
    read_function_execution,
)

TASK0C_ATTEMPTS_LOG = ".task0c_attempts.jsonl"


@dataclass(frozen=True, slots=True)
class Task0cDeps:
    BUILD_TIMEOUT: Any
    COVERAGE_TARGET: Any
    coverage_ignore_pragma: Any
    TASK0C_PROMPT_CHAR_CAP: Any
    FIX_ERR_CAP: Any
    SIBLING_CAP: Any
    SRC_SNIP_CAP: Any
    TASK0C_FIX_THINKING_DIRECTIVE: Any
    TASK0C_MODEL: Any
    TASK0C_THINKING_DIRECTIVE: Any
    WORKING_CAP: Any
    annotate_uncovered: Any
    block_is_prose: Any
    build_gen_tests_decl: Any
    cargo_env: Any
    # A generated test's gate (`application.test_worlds`): the generating
    # world's own gate while the block is being validated, then the worlds
    # cargo measured it to build and pass in.
    world_gate: Any              # (cluster) → that world's own cfg gate
    # (project_dir, module, test_file, block, cluster) → gate, or None when the
    # test does not pass in the world that generated it (the caller drops it).
    settle_test_gate: Any
    condense_rust_to_signatures: Any
    count_test_attrs: Any
    derive_gentest_prelude: Any
    describe_test_environment: Any
    error_signatures_from_output: Any
    extract_branch_guard: Any
    extract_gen_tests_body: Any
    extract_public_symbols: Any
    extract_uncovered_functions: Any
    filter_cfg_excluded_lines: Any
    filter_cfg_incompatible_lines: Any
    flags_unreachable: Any
    gen_tests_path: Any
    gen_tests_siblings: Any
    mutation_harden_file: Any
    has_nonempty_test_body: Any
    inject_allow_before_any_mod: Any
    inject_allow_before_gen_mod: Any
    inject_gen_prelude_imports: Any
    is_gen_tests_file: Any
    is_structural_line: Any
    load_file_constraints: Any
    mark_phase: Any
    match_gen_tests_sibling: Any
    next_gen_tests_n: Any
    nightly_rules_for_cluster: Any
    parse_attempt_number: Any
    parse_retrospective_categories: Any
    partition_uncovered_lines: Any
    prelude_import_names: Any
    quick_file_coverage: Any
    read_all_gen_tests: Any
    read_state: Any
    revert_gen_tests_pass: Any
    select_relevant_siblings: Any
    set_current: Any
    source_hash: Any
    strip_all_gen_tests_artifacts: Any
    strip_gen_tests_decl_n: Any
    strip_gen_tests_decls: Any
    strip_trailing_file_tag: Any
    test_augmentor_factory: Any
    type_signals_write: Any
    ask: Any
    extract_files: Any
    extract_test_mod: Any
    guard: Any
    generate_fix_loop: Any
    generate_in_parallel: Any
    is_test_file: Any
    write: Any
    subprocess: Any
    get_current_cluster: Any
    coverage_provider: Any
    check_stop: Any   # graceful-stop hook for the fix loops (was a fix_loop global)
    # Percent of the cluster's pinnable harvested behaviours the generated
    # tests must pin (float; 0 = the pin requirement and the behaviour rounds
    # are off, the public-API refusal still applies). Defaulted, last, so a
    # composition root that does not pass it yet still constructs.
    BEHAVIOUR_COVERAGE_TARGET: Any = 0.0
    # (project_dir, cluster_name) -> BehaviourSuite — the baked
    # `.skeleton/_behaviours_<cluster>.json` reader; None = no suite listed.
    load_behaviour_suite: Any = None
    # Capture/guess split (`BEHAVIOUR_CAPTURE_SPLIT`): drop the capture-owned
    # behaviour classes from what this task lists and requires a pin on.
    # Defaulted so a composition root that does not pass it still constructs.
    BEHAVIOUR_CAPTURE_SPLIT: Any = True
    # The function-execution criterion's inputs: the extractor's fn records
    # over the crate root and mod records over `src/`, and the per-cluster
    # record store (`(project_dir, cluster, module) -> FnCoverageRecord|None`
    # and its writer). Any of them absent → the reading is UNMEASURED and the
    # stop says so; nothing reads an absent reading as "all executed".
    enumerate_fns: Any = None
    enumerate_mods: Any = None
    load_fn_record: Any = None
    save_fn_record: Any = None
    other_fn_records: Any = None
    # (crate root, cluster) -> `WorldReading`: rustc's generic MIR and public
    # index for the cluster's world, from which the functions no test can
    # execute are PROVED rather than asked of a model. Absent: nothing is
    # proved and the budget behaves as it did.
    measure_reachability: Any = None
    # The per-file line floor (`COVERAGE_FILE_FLOOR`, percent over the lines a
    # test can reach; 0 = off, and the stop reads the module aggregate alone)
    # and its per-cluster store: `(project_dir, cluster, module) -> FileFloor|None`
    # and its writer.
    COVERAGE_FILE_FLOOR: Any = 0.0
    load_file_floor: Any = None
    save_file_floor: Any = None


def _iter_module_rs(module_dir: Path) -> list[Path]:
    """Real source .rs files of the module, sorted. Excludes cargo build
    artifacts (`target/`) and per-cluster skeleton scratch (`.skeleton/`):
    those trees hold duplicate-basename copies (e.g. macros.rs, context.rs)
    that false-match the `src_file.name in compile_err` basename checks and
    drag build-expanded/scratch files into the harness-fix and restore loops.
    """
    return [
        p for p in sorted(module_dir.rglob("*.rs"))
        if "target" not in p.parts and ".skeleton" not in p.parts
    ]


def _engine_get_history(deps: Task0cDeps, project_dir: Path, rel_path: str, max_entries: int = 5) -> str:
    """Return formatted prior-attempt history for rel_path, most recent first.

    Shows the full compile error (no cap). Block preview is already compressed
    to signatures by _log_task0c_attempt, so this doesn't re-truncate it.
    """
    _FIX_ERR_CAP = deps.FIX_ERR_CAP
    log_path = project_dir / TASK0C_ATTEMPTS_LOG
    if not log_path.exists():
        return ""
    entries = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("file") == rel_path:
                    entries.append(entry)
            except json.JSONDecodeError:
                pass
    except Exception:
        return ""
    if not entries:
        return ""
    recent = entries[-max_entries:]
    n = len(recent)
    has_successes = any(e.get("outcome") == "success" for e in recent)
    parts = [f"Attempt history for `{rel_path}` ({n} attempt{'s' if n != 1 else ''}):"]
    for e in recent:
        outcome = e.get("outcome", "unknown")
        err = e.get("compile_error", "")
        preview = e.get("block_preview", "")
        result_label = "compiled and is COMMITTED TO DISK" if outcome == "success" else f"FAILED ({outcome})"
        entry_lines = [f"Attempt {e.get('attempt', '?')}: → {result_label}"]
        if preview:
            entry_lines.append(f"Test signatures:\n{preview}")
        if err:
            err_capped = err[-_FIX_ERR_CAP:] if len(err) > _FIX_ERR_CAP else err
            entry_lines.append(f"Compiler said:\n{err_capped}")
        if outcome == "success":
            entry_lines.append(
                "→ These tests ARE ALREADY COMMITTED as a `tests/{stem}_gen_N.rs` "
                "integration file. Do NOT include them in your new block — they "
                "are already active as their own cargo test binary."
            )
        else:
            entry_lines.append("→ This approach did not work. Do NOT repeat it.")
        parts.append("\n".join(entry_lines))
    if has_successes:
        parts.append(
            "The committed tests above are already on disk as `tests/{stem}_gen_N.rs` "
            "files. Do NOT copy or repeat them. Write ONLY NEW test functions "
            "targeting the uncovered lines shown in fn_bodies below."
        )
    else:
        parts.append("Write a new attempt that avoids every failure pattern above.")
    return "\n\n".join(parts)


def _engine_pragma_pass(
    deps: Task0cDeps,
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    files_data: list[PerFileCoverage],
    model: str,
) -> None:
    """
    Called when coverage stalls: ask the LLM to add `// # tarpaulin coverage ignore`
    to lines that are genuinely unreachable from inline #[cfg(test)] blocks
    (cfg-gated nightly code, unsafe vtable dispatch, unreachable!() arms, etc.).

    This is legitimate — production codebases routinely exclude intrinsically
    unreachable paths from coverage measurement. Tarpaulin honours the pragma and
    stops counting those lines, allowing coverage to reach 100% on the coverable
    portion.
    """
    BUILD_TIMEOUT = deps.BUILD_TIMEOUT
    COVERAGE_IGNORE_PRAGMA = deps.coverage_ignore_pragma
    _annotate_uncovered = deps.annotate_uncovered
    _cargo_env = deps.cargo_env
    _log = log
    ask = deps.ask
    extract_files = deps.extract_files
    is_test_file = deps.is_test_file
    subprocess = deps.subprocess
    mod_prefix = str(module_dir) + "/"

    # Build map: abs_path → set of uncovered line numbers
    uncovered_by_file: dict[Path, set[int]] = {}
    for fdata in files_data:
        abs_path = Path(fdata.abs_path)
        if not str(abs_path).startswith(mod_prefix):
            continue
        if is_test_file(abs_path, project_dir):
            continue
        if abs_path.name == "build.rs":
            continue
        lines_uncov = {
            t["line"] for t in fdata.traces
            if t["stats"].get("Line", 1) == 0
        }
        if lines_uncov:
            uncovered_by_file[abs_path] = lines_uncov

    if not uncovered_by_file:
        return

    for abs_path, uncov_lines in uncovered_by_file.items():
        rel = str(abs_path.relative_to(project_dir))
        try:
            source = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue

        annotated = _annotate_uncovered(source, uncov_lines)
        prompt = f"""The following Rust source file has lines that remain uncovered after multiple
test-generation attempts. These lines may be genuinely unreachable from the crate's public API (the generated tests live in `tests/`
and can only call `pub` items) — for example:
- Code behind `#[cfg(error_generic_member_access)]` or other non-default cfg flags
- `unsafe` functions only called via vtable dispatch (never called directly in tests)
- `unreachable!()` arms in exhaustive matches
- Lines only reachable via OS-level error conditions

Your task: add `{COVERAGE_IGNORE_PRAGMA}` to each line that is intrinsically
unreachable from a `tests/` integration test of the public API.

Rules:
- ONLY add the pragma to lines marked `// !! UNCOVERED` that you can confirm are
  unreachable from the public API. Do NOT add it to lines that could be covered
  by a test.
- Do NOT modify any existing test code or logic.
- If a line is coverable (just needs a better test), do NOT add the pragma — leave
  it for the next generation attempt.
- Output the complete corrected file.

File: {rel}
```rust
{annotated}
```

<FILE path="{rel}">
corrected content here
</FILE>
"""
        response = ask(prompt, model=model)
        files_out = extract_files(response)
        if rel in files_out:
            new_content = files_out[rel]
            # Sanity: must still compile before writing.
            tmp = abs_path.with_suffix(".pragma_tmp.rs")
            try:
                tmp.write_text(new_content, encoding="utf-8")
                tmp.rename(abs_path)
                check = subprocess.run(
                    ["cargo", "test", "--no-run", "-p", module_name],
                    cwd=project_dir, capture_output=True, text=True,
                    timeout=BUILD_TIMEOUT, env=_cargo_env(),
                )
                if check.returncode != 0:
                    _log(f"  [pragma-pass] {rel}: compile failed after pragma addition — reverting.")
                    abs_path.write_text(source, encoding="utf-8")
                else:
                    added = new_content.count(COVERAGE_IGNORE_PRAGMA) - source.count(COVERAGE_IGNORE_PRAGMA)
                    _log(f"  [pragma-pass] {rel}: added {added} coverage ignore pragma(s).")
            except Exception as e:
                _log(f"  [pragma-pass] {rel}: error — {e}. Reverting.")
                with suppress(Exception):
                    abs_path.write_text(source, encoding="utf-8")
        else:
            _log(f"  [pragma-pass] {rel}: LLM returned no output — skipping.")


def _looks_like_fn_decl(ln: str) -> bool:
    """Regex-free: does this line start a Rust fn declaration?
    Equivalent to the old re.match(r'\\s*(pub\\s+)?fn\\s+', ln) used to count
    uncovered functions: optional leading ws, optional `pub`, then `fn` followed
    by whitespace and at least a name token."""
    toks = ln.split()
    if toks[:1] == ["fn"]:
        return len(toks) > 1
    if toks[:2] == ["pub", "fn"]:
        return len(toks) > 2
    return False


def _engine_retrospective(
    deps: Task0cDeps,
    rel_path: str,
    existing_text: str,
    fn_bodies: str,
    old_block: str,
    module_name: str,
    attempt_history: str = "",
    uncov_lines: set[int] | None = None,
    unproductive_blocks: list[str] | None = None,
    consecutive_giveups: int = 0,
) -> str:
    """Ask LLM to classify why remaining uncovered functions can't be reached.

    Returns a structured per-function classification to inject into the next attempt
    so the model knows whether to try a different approach or skip with a pragma.

    When `unproductive_blocks` is non-empty, the classifier is told that those
    specific gen_tests blocks have already been written, compiled and run, and
    did NOT improve coverage of the targeted lines. The LLM is then asked to
    revise any earlier TESTABLE call -- if multiple concrete attempts failed
    to move the metric, the line is probably structurally unreachable in this
    test environment.
    """
    _SRC_SNIP_CAP = deps.SRC_SNIP_CAP
    _TASK0C_MODEL = deps.TASK0C_MODEL
    _describe_test_environment = deps.describe_test_environment
    _extract_branch_guard = deps.extract_branch_guard
    ask = deps.ask
    if not fn_bodies.strip():
        return ""
    # Count the distinct function names so we can require all to be classified.
    _fn_count = sum(1 for ln in fn_bodies.splitlines() if _looks_like_fn_decl(ln))
    _src_snippet = existing_text[:_SRC_SNIP_CAP]
    _block_snippet = old_block[:_SRC_SNIP_CAP // 2] if old_block else "(no previous block)"
    _history_section = (
        f"Compile error history for this file (what went wrong in previous attempts):\n{attempt_history}\n\n"
    ) if attempt_history.strip() else ""
    _count_line = (
        f"There are {_fn_count} function(s) with uncovered lines. "
        f"You MUST output exactly one FUNCTION line for EVERY function — do not skip any.\n\n"
    ) if _fn_count > 0 else ""
    # Build branch-guard hints for each uncovered line so the retrospective LLM
    # knows exactly what condition guards each line — improves TESTABLE vs UNREACHABLE accuracy.
    _guard_section = ""
    if uncov_lines:
        _src_lines = existing_text.splitlines()
        _guard_hints = []
        for _lno in sorted(uncov_lines):
            g = _extract_branch_guard(_src_lines, _lno)
            if g:
                _guard_hints.append(f"  line {_lno}: guarded by `{g}`")
        if _guard_hints:
            _guard_section = "Branch conditions for uncovered lines:\n" + "\n".join(_guard_hints) + "\n\n"
    # When called with evidence of prior failed attempts, surface them and ask
    # for reclassification with that evidence in mind.
    _unproductive_section = ""
    if unproductive_blocks:
        _BLOCK_CAP = max(800, _SRC_SNIP_CAP // 4)
        _shown = []
        for i, blk in enumerate(unproductive_blocks, 1):
            _shown.append(f"--- attempt {i} (compiled, ran, did NOT improve coverage of the targeted lines) ---\n"
                          f"{blk[:_BLOCK_CAP]}")
        _unproductive_section = (
            "PRIOR FAILED ATTEMPTS — these blocks compiled and ran but did NOT increase\n"
            "coverage of the still-uncovered lines. Treat them as concrete evidence that\n"
            "the strategy used to write them does NOT work in this test environment. If\n"
            "the same target was previously classified TESTABLE, the classification was\n"
            f"wrong — revise it now.\n\n{chr(10).join(_shown)}\n\n"
        )
    # The test-generation model itself has repeatedly declared these lines unreachable. That is
    # the strongest signal there's no public-API test path — weigh it heavily toward UNREACHABLE.
    _giveup_section = ""
    if consecutive_giveups >= 2:
        _giveup_section = (
            f"STRONG SIGNAL: the test-generation model has, on {consecutive_giveups} consecutive "
            "attempts, judged these lines to have NO reachable test path (it returned an empty "
            "block citing unreachability). If you cannot point to a concrete public-API call that "
            "would execute a line, classify it UNREACHABLE — do not keep it TESTABLE.\n\n"
        )
    _env_section = _describe_test_environment()
    prompt = (
        f"You generated tests for `{rel_path}` but some lines remain uncovered.\n"
        f"{_count_line}"
        f"For each function still marked `// !! UNCOVERED` below, classify it and suggest what to try.\n\n"
        f"{_env_section}\n"
        f"{_guard_section}"
        f"{_history_section}"
        f"{_unproductive_section}"
        f"{_giveup_section}"
        f"Previous test block (already committed):\n```rust\n{_block_snippet}\n```\n\n"
        f"Still-uncovered functions:\n```rust\n{fn_bodies}\n```\n\n"
        f"Source context:\n```rust\n{_src_snippet}\n```\n\n"
        f"For each uncovered function, output exactly one line:\n"
        f"FUNCTION <name>: <category> — <one sentence reason or concrete suggestion>\n\n"
        f"Categories:\n"
        f"(a) TESTABLE — coverable with a different approach; describe the specific approach\n"
        f"(b) CFG_GATED — inside #[cfg(feature=...)] or excluded by the test environment above; untestable in this run\n"
        f"(c) UNREACHABLE — unreachable!() arm, dead branch, or intrinsically dead code; add tarpaulin ignore\n"
        f"(d) NEEDS_INTERNALS — requires private state, unsafe vtable dispatch, env vars not set in the parent process, or internal construction not accessible from the crate's public API\n\n"
        f"Before you classify a function (c) or (d), name the public call that would NOT reach it. "
        f"A trait-impl method has no caller by name: it runs whenever the trait is dispatched on a value "
        f"of its type, including a value handed out as `dyn Trait` or `Box<dyn Error>` and formatting "
        f"with `{{:?}}`, `{{:#?}}` or `{{}}`. \"Nothing in the crate calls it\" is not a reason; "
        f"a type that no public item can ever return is.\n"
    )
    result = ask(prompt, model=_TASK0C_MODEL, think=None)
    lines = [ln.strip() for ln in result.splitlines() if ln.strip().startswith("FUNCTION")]
    return "\n".join(lines) if lines else result[:600].strip()


def _engine_targeted_pragma(
    deps: Task0cDeps,
    project_dir: Path,
    module_name: str,
    rel_path: str,
    uncov_lines: set[int],
    pragma_functions: list[str],
    model: str,
) -> bool:
    """Add tarpaulin ignore pragmas to specific classified-unreachable functions.

    Reads the current file state (including any committed gen_tests block), asks the
    LLM to add pragmas only to the listed functions, then compile-checks before
    writing.  Returns True on success.
    """
    BUILD_TIMEOUT = deps.BUILD_TIMEOUT
    COVERAGE_IGNORE_PRAGMA = deps.coverage_ignore_pragma
    _annotate_uncovered = deps.annotate_uncovered
    _cargo_env = deps.cargo_env
    _log = log
    _strip_gen_tests_decls = deps.strip_gen_tests_decls
    ask = deps.ask
    extract_files = deps.extract_files
    subprocess = deps.subprocess
    abs_path = project_dir / rel_path
    try:
        current_source = abs_path.read_text(encoding="utf-8")
    except Exception:
        return False

    # Strip gen_* blocks before sending to LLM — files can become very large after many
    # augmentation rounds, causing the LLM to fail to return the complete file.
    # We reattach the stripped suffix after the LLM edits only the core source.
    # gen_tests bodies now live in sibling files (foo.gen_tests_N.rs); the source
    # file only contains short `mod gen_tests_N;` path-mod declarations. Strip those
    # before sending to the LLM and reattach the exact same bytes afterwards.
    _core_source = _strip_gen_tests_decls(current_source).rstrip() + "\n"
    _gen_suffix   = current_source[len(_core_source.rstrip("\n")):]

    annotated = _annotate_uncovered(_core_source, uncov_lines)
    fn_list = "\n".join(f"- {fn}" for fn in pragma_functions)

    prompt = (
        f"The following functions in `{rel_path}` have been classified as intrinsically "
        f"unreachable from inline `#[cfg(test)]` unit tests "
        f"(compile-time dispatch markers, cfg-gated nightly code, dead branches):\n"
        f"{fn_list}\n\n"
        f"Add `{COVERAGE_IGNORE_PRAGMA}` to ONLY the lines marked `// !! UNCOVERED` "
        f"that belong to these specific functions. "
        f"Do NOT add the pragma to any other line. Do NOT change any other code.\n\n"
        f"Source file (gen_* test blocks omitted for brevity):\n```rust\n{annotated}\n```\n\n"
        f"Output the COMPLETE corrected source (without the gen_* blocks) wrapped exactly "
        f"like this — no other text:\n"
        f'<FILE path="{rel_path}">\n'
        f"(complete file content here)\n"
        f"</FILE>"
    )

    response = ask(prompt, model=model, think=None)
    files_out = extract_files(response)
    if rel_path not in files_out:
        # Fallback: extract from the largest ```rust ... ``` code block.
        code_blocks = extract_code_blocks(response)
        if code_blocks:
            candidate = max(code_blocks, key=len).strip()
            if len(candidate) > 100:
                files_out = {rel_path: candidate}
                _log(f"  [targeted-pragma] {rel_path}: fell back to code-block extraction.")
    if rel_path not in files_out:
        # Last-resort: programmatically insert pragma on the exact uncovered lines.
        _lines = _core_source.splitlines(keepends=True)
        _added = 0
        for _lno in sorted(uncov_lines):
            if 1 <= _lno <= len(_lines) and COVERAGE_IGNORE_PRAGMA not in _lines[_lno - 1]:
                _lines[_lno - 1] = _lines[_lno - 1].rstrip("\n") + f"  {COVERAGE_IGNORE_PRAGMA}\n"
                _added += 1
        if _added:
            files_out = {rel_path: "".join(_lines)}
            _log(f"  [targeted-pragma] {rel_path}: LLM failed — applied {_added} pragma(s) directly.")
        else:
            _log(f"  [targeted-pragma] {rel_path}: LLM returned no output.")
            return False

    # Strip any // !! UNCOVERED annotations the LLM may have copied from the annotated
    # prompt back into the output — these are prompt-only markers and must not persist
    # in the source file.
    _raw_llm_out = files_out[rel_path]
    _cleaned_out = strip_uncovered_markers(_raw_llm_out)
    if _cleaned_out != _raw_llm_out:
        files_out[rel_path] = _cleaned_out
        _log(f"  [targeted-pragma] {rel_path}: stripped leaked // !! UNCOVERED annotations from LLM output.")

    # Ensure all uncov_lines actually have the pragma — the LLM sometimes returns the
    # file unchanged.  Apply any missing pragmas programmatically to the LLM's output.
    _out_lines = files_out[rel_path].splitlines(keepends=True)
    _missed = 0
    for _lno in sorted(uncov_lines):
        if 1 <= _lno <= len(_out_lines) and COVERAGE_IGNORE_PRAGMA not in _out_lines[_lno - 1]:
            _out_lines[_lno - 1] = _out_lines[_lno - 1].rstrip("\n") + f"  {COVERAGE_IGNORE_PRAGMA}\n"
            _missed += 1
    if _missed:
        files_out[rel_path] = "".join(_out_lines)
        _log(f"  [targeted-pragma] {rel_path}: LLM missed {_missed} pragma(s) — added programmatically.")

    # Reattach gen_* suffix so no accumulated tests are lost.
    _core_out = files_out[rel_path].rstrip("\n") + "\n"
    new_content = _core_out + _gen_suffix
    abs_path.write_text(new_content, encoding="utf-8")
    check = subprocess.run(
        ["cargo", "test", "--no-run", "-p", module_name],
        cwd=project_dir, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT, env=_cargo_env(),
    )
    if check.returncode != 0:
        _log(f"  [targeted-pragma] {rel_path}: compile failed after pragma addition — reverting.")
        abs_path.write_text(current_source, encoding="utf-8")
        return False

    added = new_content.count(COVERAGE_IGNORE_PRAGMA) - current_source.count(COVERAGE_IGNORE_PRAGMA)
    _log(f"  [targeted-pragma] {rel_path}: added {added} pragma(s) for {len(pragma_functions)} classified function(s).")
    return True


_COMPILE_FIX_HEAD = """The Rust test block you generated for `{rel_path}` does not compile.
    {directive}Fix ONLY the test block so it compiles. Return the corrected <FILE path="gen_tests_block"> block.
    {history}
    Compile error (read carefully — this is the ONLY thing to fix):
    {err}
"""

_RUNTIME_FIX_HEAD = """The Rust test block you generated for `{rel_path}` COMPILES, but some of its own
    tests FAIL when run against the implementation in that file. That implementation is the
    specification here: your EXPECTATION is wrong, the code is not. Correct the assertions to
    match what the code actually produces, then return the corrected <FILE path="gen_tests_block"> block.
    {directive}{history}
    Failing tests — actual panic output, read the `left`/`right` values:
    {err}

    WRONG — guessing an expectation, then asserting it:
        assert_eq!(render(&v), "value: 3");   // panics: left: `"3"`, right: `"value: 3"`
    RIGHT — assert what the code produced:
        assert_eq!(render(&v), "3");

    WRONG — asserting a wrapper adds a prefix it never adds:
        assert_eq!(Wrapper::new(inner).to_string(), "wrapped: boom");   // panics: left: `"boom"`
    RIGHT — assert the observed forwarding:
        assert_eq!(Wrapper::new(inner).to_string(), "boom");

    WRONG — silencing the failure instead of fixing it:
        #[ignore]                     // also: #[should_panic], or gutting the body to assert!(true)
        fn test_render() {{ /* … */ }}
    RIGHT — keep the same call, narrow the claim to what the output supports:
        assert!(render(&v).contains('3'));

    Rules: keep the same calls into the code under test; never add `#[ignore]` or `#[should_panic]`
    to turn a red test green; a test you cannot make honestly true may be dropped entirely, but
    never kept as a tautology.
"""

_CONTRACT_FIX_HEAD = """The Rust test block you generated for `{rel_path}` COMPILES and its tests PASS, but it
    was REFUSED by the test contract. Rewrite the block so it satisfies the contract — keep the
    same calls into the code under test — then return the corrected <FILE path="gen_tests_block"> block.
    {directive}{history}
    Refusal (read carefully — this is the ONLY thing to fix):
    {err}

    The contract: every test pins at least one listed harvested behaviour by asserting its EXACT
    observable (`assert_eq!` against the whole listed `then`, or the `is::<T>()` identity form);
    `contains(...)` / `.is_ok()` / `.unwrap()` alone pin nothing; and the block uses the PUBLIC API
    only — no private fields, fns or types. Private items are not in scope
    from `tests/` (`use crate::*`); a leftover inline `use super::*;` block
    that names one is still refused: a test of an internal freezes a layout
    the reconstruction is free to redesign.
"""

# The generation contract, verbatim in the generator prompt, the medium
# retry and the fix prompt, and enforced by `judge_block_contract` at the
# admission site. It replaced "simply exercising the code path is acceptable":
# on the 2026-08 anyhow trees that sentence produced admitted blocks asserting
# `format!("{:?}", x).contains("…")` where the crate's own tests pin the whole
# rendering, and tests reading private fields (24 % of the private surface).
_TEST_CONTRACT = """Test contract — each test is judged against it after it compiles and passes; a block that fails it is refused:
- PIN a behaviour: each test must pin at least one listed harvested behaviour (the "Harvested behaviours" section above, when present) by asserting its EXACT observable — `assert_eq!` against the listed `then` (the whole string, byte for byte; a `const EXPECTED: &str = "…";` is fine for multi-line values), or for a type identity the `assert!(e.is::<T>())` / `e.downcast_ref::<T>().is_some()` form. Where NO behaviour is listed, do not try to predict an exact rendering you have not been shown — reach the uncovered lines through the public API and assert what you can establish from the source you were given.
- `contains(...)`, `.is_ok()`, `.is_some()`, `.is_err()`, `.unwrap()` or a bare call ALONE are not accepted as a test's assertion — they pin nothing an alternative implementation could violate.
- PUBLIC API ONLY: use only `pub` fns, types and fields. No private fields, no private fns, no private types. These tests live in `tests/` as an integration-test crate (`use <crate>::*`); private items are not in scope, and a leftover inline block that names one is refused: a test of an internal freezes a layout the reconstruction is free to redesign.
- Line coverage is secondary: also reach the uncovered lines shown in fn_bodies, through the public API.
- Prefer edge cases and error conditions, not just happy paths.
- When testing a function that returns `Result` or `Option`, test BOTH the Ok/Some path
  AND the Err/None path if the code shows both branches.
"""


def _integration_test_rules(*, crate_name: str, prelude_names: str) -> str:
    """The placement contract: generated tests live in `tests/`, not as an
    inline `#[cfg(test)]` module with `use super::*`. That inline placement
    is what granted private access and froze 24 % of anyhow's private
    surface (2026-08); a visibility predicate against it fights the
    generator's own contract."""
    return f"""Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- These tests live in `tests/` as an integration-test crate, not inside the source file.
- Use `use {crate_name}::*;` (or a narrower `use {crate_name}::…`) for the public API. Do NOT write `use super::*;` — there is no parent module, and private items are not in scope.
- Read field types from the struct definitions in the source file — match EXACTLY, and only `pub` fields.

INTEGRATION TEST RULES — these tests live in `tests/`, an external test crate:
- Do NOT write `use super::*;`.
- The template prelude already imports: {prelude_names} — use them directly.
- Do NOT use `#[macro_export]` macros that are not part of the public API.
  Construct values through public API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
"""


def _engine_runtime_gate(
    *,
    project_dir: Path,
    module_name: str,
    test_bin: str,
    block: str,
    subprocess: Any,
    cargo_env: Callable[[], dict[str, str]],
    timeout: int,
) -> BlockAdmission:
    """Run ONLY this block's own tests against the implementation they specify.

    Augmentation authors tests while that implementation is still on disk, so a
    block's expectations are checkable the moment it is written. Without this a
    compiling block whose assertion is simply wrong enters the reconstruction
    oracle as an entry no fill can ever satisfy, and the fns it covers are
    re-attempted for the life of the project.

    The run is filtered to this file's cargo `--test` binary (`{stem}_gen_{n}`),
    which is what lets a missing verdict be read as "this block crashed the
    binary" rather than as someone else's failure.
    """
    run = subprocess.run(
        ["cargo", "test", "-p", module_name, "--test", test_bin, "--",
         "--test-threads=1"],
        cwd=project_dir, capture_output=True, text=True,
        timeout=timeout, env=cargo_env(),
    )
    return judge_block(block, (run.stdout or "") + (run.stderr or ""),
                       run_ok=run.returncode == 0)


def _engine_mutation_pass(
    deps: Task0cDeps,
    project_dir: Path,
    module_name: str,
    cluster: Any,
    rel_files: list[str],
) -> int:
    """Behaviour-hardening pass over a cluster's source files. For each file
    with surviving mutants, generate black-box tests and persist the kept
    ones as a `tests/{stem}_gen_N.rs` integration file so they survive strip
    and reattach per-cluster. Opt-in via TASK0C_MUTATION_HARDEN=1 (mutation-kill as the strength
    metric on top of coverage). Returns the number of files hardened.
    """
    _gen_tests_siblings = deps.gen_tests_siblings
    _log = log
    _mutation_harden_file = deps.mutation_harden_file
    if os.getenv("TASK0C_MUTATION_HARDEN", "0").strip() in ("", "0", "false"):
        return 0
    hardened = 0
    for rel in rel_files:
        src = project_dir / rel
        if not src.exists():
            continue
        try:
            code = _mutation_harden_file(project_dir, module_name, cluster, rel)
        except Exception as e:
            _log(f"  [mut-harden] {rel}: skipped ({e})")
            continue
        if not code.strip():
            continue
        # persist as the next tests/{stem}_gen_N.rs integration file
        existing = _gen_tests_siblings(src)
        n = (max((k for k, _ in existing), default=0)) + 1
        tests_dir = project_dir / "tests"
        if tests_dir.is_dir():
            for f in tests_dir.iterdir():
                parsed = parse_gen_tests_integration_filename(f.name)
                if parsed and parsed[0] == src.stem:
                    n = max(n, parsed[1] + 1)
        dest = project_dir / gen_tests_integration_rel(src.stem, n)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            "// mutation-hardening tests — behaviour, not coverage\n"
            + as_integration_test(code, crate=module_name),
            encoding="utf-8")
        hardened += 1
        _log(f"  [mut-harden] {rel}: persisted hardening tests as {dest.name}")
    return hardened


def _engine_log_attempt(
    deps: Task0cDeps,
    project_dir: Path, rel_path: str, attempt_n: int,
    block: str, compile_error: str, outcome: str,
) -> None:
    """BL-002/BL-016: Append one TASK 0c attempt record to the per-project JSONL log.

    BL-016: Compress the failed test block to function signatures only (saves space)
    so the full compile error can be stored uncapped.
    """
    _condense_rust_to_signatures = deps.condense_rust_to_signatures
    type_signals_write = deps.type_signals_write
    log_path = project_dir / TASK0C_ATTEMPTS_LOG
    # BL-016: compress block to signatures — frees budget for full compile error.
    block_preview = ""
    if block:
        condensed = _condense_rust_to_signatures(block, filename="gen_tests.rs")
        block_preview = condensed[:600] if condensed else block[:300]
    entry = {
        "file": rel_path,
        "attempt": attempt_n,
        "block_preview": block_preview,
        "compile_error": compile_error,  # BL-016: no cap — full error stored
        "outcome": outcome,  # "success", "compile_error", "test_failure", "no_block", "reverted"
        "ts": datetime.now(UTC).isoformat(),
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # BL-022: if this is a compile error, extract type names for type_signals.
    if outcome == "compile_error" and compile_error:
        for type_name, _idx in error_type_signals(compile_error):
            pattern = compile_error[max(0, _idx - 30):_idx + 80].strip()
            type_signals_write(project_dir, rel_path, type_name, difficulty=1, pattern=pattern)


def _render_unexecuted_fns(source_text: str, targets: tuple[FnTarget, ...]) -> str:
    """The bodies of functions no test executes, numbered as the coverage run
    numbers them, each headed with its site and its cfg world. Read from the
    workspace's source, which augmentation runs on before strip."""
    if not targets:
        return ""
    lines = source_text.splitlines()
    blocks: list[str] = []
    for tgt in targets:
        world = (" — compiled only under this cluster's cfg" if tgt.cluster_gated
                 else " — shared code, compiled in every cluster")
        body = "\n".join(
            f"{n:4d} | {lines[n - 1]}"
            for n in range(max(1, tgt.first_line), min(tgt.last_line, len(lines)) + 1))
        route = trait_dispatch_route(tgt.site.container)
        via = (f"// Reached through the trait, not by a direct call: {route}\n"
               if route else "")
        blocks.append(f"// NEVER EXECUTED by any test: {tgt.label}{world}\n{via}{body}")
    return "\n\n".join(blocks)


def _calls_clause(targets: tuple[FnTarget, ...]) -> str:
    """The instruction's half for never-executed functions ("" without any)."""
    if not targets:
        return ""
    return (", and calling every function listed as NEVER EXECUTED ("
            + ", ".join(f"`{tgt.site.fn_name}`" for tgt in targets) + ")")


def _coverage_status_line(coverage: float, *, line_targets: int,
                          fn_targets: tuple[FnTarget, ...],
                          whole_file: bool) -> str:
    """The "Coverage status:" sentence for a prompt carrying exactly these
    targets, and no others.

    Split-and-merge sends one request per subset of a file's uncovered lines,
    and each request renders only its own fn_bodies and NEVER EXECUTED blocks.
    A sentence built once from the file's totals and reused across the subsets
    states a count the prompt under it contradicts: on run 3044-3c9670bf
    (2026-09-17) 45 of the 54 task0c prompts carrying the never-executed clause
    named more functions than they listed, and three named one function while
    listing none and carrying no call instruction at all. So the counts come
    from the caller's own targets, and `whole_file` decides whether the file's
    other lines may be described as already covered — for a subset they are
    another concurrent request's targets, not covered.
    """
    if coverage <= 0:
        return ""
    head = f"\nCoverage status: {coverage:.1f}% covered. "
    if not line_targets and not fn_targets:
        return head.rstrip() + "\n"
    scope = ("are the ONLY remaining targets in this file — all other lines "
             "are already covered."
             if whole_file else
             "are the ONLY targets of this request — the file's other uncovered "
             "lines are targeted by a separate request running beside this one.")
    return (
        head
        + (f"The {line_targets} line(s) in fn_bodies below " if line_targets else "")
        + ("and " if line_targets and fn_targets else "")
        + (f"{'the' if line_targets else 'The'} {len(fn_targets)} function(s) "
           "listed as never executed " if fn_targets else "")
        + scope + " Focus exclusively on these.\n"
    )


def _extract_existing_tests(source_text: str) -> str:
    """Extract the existing #[cfg(test)] mod tests block from a source file.

    Showing the existing tests as an example prevents the model from inventing
    types that don't exist: it can see exactly how to construct e.g. CommandInfo
    by looking at test code that already compiles correctly.

    Only extracts `mod tests` (the original hand-written tests), NOT `mod gen_tests`
    (our generated block) — gen_tests may contain broken code that would teach the
    model to hallucinate wrong types if used as an example.
    """
    # Find `mod tests {` specifically, not `mod gen_tests`.
    # Return the full block — a truncated example ends mid-function and teaches the model
    # wrong patterns, causing compile failures in generated tests.
    for marker in ("#[cfg(test)]\nmod tests", "#[cfg(test)]\npub mod tests"):
        if marker in source_text:
            idx = source_text.index(marker)
            return source_text[idx:].strip()
    return ""


def _engine_build_prompt_sections(
    deps: Task0cDeps,
    rel_path: str,
    existing_text: str,
    fn_bodies: str,
    pub_symbols: str,
    cargo_toml_content: str,
    integration_tests: list[Any],
    sibling_files: list[Any],
    coverage_strategy: str,
    attempt_history: str,
    project_context: str,
    old_block: str = "",
    what_worked: str = "",
    retrospective: str = "",
    uncov_lines: set[int] | None = None,
    scaffold_chars: int = 0,
    behaviours: str = "",
    unexecuted_fns: str = "",
) -> dict[str, Any]:
    """
    BL-007: Build context sections for one TASK 0c per-file prompt.

    Prioritized degradation — if total exceeds budget, drop lowest-priority sections first:
      1. Always: Cargo.toml + full source + uncovered fn bodies + public symbols
      2. Add if fits: coverage_strategy, attempt_history, existing inline tests, project context
      3. Add if fits: integration tests (shortest first)
      4. Add if fits: sibling source files (shortest first)

    Returns dict of section_name -> content string.

    `scaffold_chars` (optional): caller-measured size of the wrapping prompt
    text excluding section content. When supplied, used directly as the
    overhead — eliminates the magic 8_000 fallback.
    """
    TASK0C_PROMPT_CHAR_CAP = deps.TASK0C_PROMPT_CHAR_CAP
    _guard = deps.guard
    OVERHEAD = scaffold_chars if scaffold_chars > 0 else 8_000
    budget = TASK0C_PROMPT_CHAR_CAP - OVERHEAD
    dropped: list[str] = []

    # --- Always-included (priority 1) ---
    cargo_section = (
        f"\nCargo.toml (available dependencies — ONLY use what is listed here):\n"
        f"```toml\n{cargo_toml_content}\n```\n"
    ) if cargo_toml_content.strip() else ""

    # Cap full source at 50% of budget — macro-heavy files (ensure.rs 50k, error.rs 35k)
    # otherwise consume the entire budget leaving no room for siblings or fn_bodies.
    # The fn_bodies section already captures all uncovered code; full source adds import
    # and type context. Truncation preserves the top of the file (imports, type defs).
    _src_cap = int(budget * 0.50)
    if len(existing_text) > _src_cap:
        _guard("task0c/source_cap", f"{rel_path}: {len(existing_text):,} chars → capped at {_src_cap:,} (50% budget)")
    if len(existing_text) <= _src_cap:
        _src_text = existing_text
    else:
        _cut = existing_text.rfind("\n", 0, _src_cap)
        _src_text = existing_text[:_cut if _cut != -1 else _src_cap] + "\n// ... (source truncated — see fn_bodies below for uncovered code)\n"
    full_source_section = (
        f"\nFull source file `{rel_path}` (file you are augmenting — note all `use` statements):\n"
        f"```rust\n{_src_text}\n```\n"
    )

    _fn_cap = int(budget * 0.40)
    if len(fn_bodies) <= _fn_cap:
        _fn_text = fn_bodies
    else:
        _sep = "\n\n// --- next function ---\n\n"
        _cutoff = fn_bodies.rfind(_sep, 0, _fn_cap)
        if _cutoff == -1:
            # Only one function and it's over budget — truncate at last newline
            _cutoff = fn_bodies.rfind("\n", 0, _fn_cap)
        _fn_text = fn_bodies[:_cutoff] + "\n\n// ... (fn_bodies truncated — remaining functions omitted)\n"
    fn_bodies_section = (
        f"\nFunctions with uncovered lines (complete bodies, `// !! UNCOVERED` marks gaps):\n"
        f"```rust\n{_fn_text}\n```\n"
    ) if fn_bodies.strip() else ""

    # Functions the coverage run says NO test enters. Priority 1 and shown
    # before the line gaps: a whole body no test reaches is the gap a stubbed
    # reconstruction survives every gate with.
    if len(unexecuted_fns) <= _fn_cap:
        _unexec_text = unexecuted_fns
    else:
        _cut_u = unexecuted_fns.rfind("\n\n", 0, _fn_cap)
        _unexec_text = (unexecuted_fns[:_cut_u if _cut_u != -1 else _fn_cap]
                        + "\n\n// ... (remaining never-executed functions omitted)\n")
    unexecuted_fns_section = (
        "\nFunctions NO test executes — the coverage run entered none of these bodies. "
        "Write tests that call each of them at least once, through the public API that "
        "reaches it; a test that runs without entering them does not count for them:\n"
        f"```rust\n{_unexec_text}\n```\n"
    ) if unexecuted_fns.strip() else ""

    symbols_section = (
        f"\nPublic API of `{rel_path}` (use ONLY these — do not invent types or functions):\n"
        f"```rust\n{pub_symbols}\n```\n"
    ) if pub_symbols.strip() else ""

    # Harvested behaviours the file has not pinned yet — priority 1, never
    # dropped: the renderer caps the section itself, and a block is held to
    # pinning one of them, so a prompt that lost the list would be refused
    # for not doing what it was never shown.
    behaviours_section = behaviours if behaviours.strip() else ""

    used = (len(cargo_section) + len(full_source_section) + len(fn_bodies_section)
            + len(unexecuted_fns_section) + len(symbols_section) + len(behaviours_section))
    remaining = budget - used

    # --- Optional sections (priority 2) ---
    # History first — most valuable feedback; strategy is generic guidance.
    history_section = ""
    if attempt_history.strip():
        s = f"\n{attempt_history}\n"
        if len(s) <= remaining:
            history_section = s
            remaining -= len(s)
        else:
            dropped.append("attempt_history")

    what_worked_section = ""
    if what_worked.strip():
        s = f"\nPatterns that achieved coverage improvement in previous attempts (reuse these constructs):\n{what_worked}\n"
        if len(s) <= remaining:
            what_worked_section = s
            remaining -= len(s)
        else:
            dropped.append("what_worked")

    retrospective_section = ""
    if retrospective.strip():
        s = f"\nRetrospective — why previous attempt did not improve coverage (act on this):\n{retrospective}\n"
        if len(s) <= remaining:
            retrospective_section = s
            remaining -= len(s)
        else:
            dropped.append("retrospective")

    strategy_section = ""
    if coverage_strategy.strip():
        # Strip raw <think>...</think> blocks — the strategy may have been produced by a
        # thinking model and the reasoning tokens must not occupy the budget slot.
        _strategy_clean = strip_think_blocks(coverage_strategy).strip()
        s = f"\nProject coverage strategy (import paths, type construction, test conventions):\n{_strategy_clean}\n"
        if len(s) <= remaining:
            strategy_section = s
            remaining -= len(s)
        else:
            dropped.append("coverage_strategy")

    existing_tests = _extract_existing_tests(existing_text)
    examples_section = ""
    if existing_tests.strip():
        s = (f"\nExisting tests in this file "
             f"(reference for correct type construction and coding style):\n"
             f"```rust\n{existing_tests}\n```\n")
        if len(s) <= remaining:
            examples_section = s
            remaining -= len(s)
        else:
            dropped.append("existing_inline_tests")

    context_section = ""
    if project_context.strip():
        s = f"\nProject context:\n{project_context}\n"
        if len(s) <= remaining:
            context_section = s
            remaining -= len(s)
        else:
            dropped.append("project_context")

    old_block_section = ""
    if old_block.strip():
        s = (
            f"\nExisting test module(s) for this file — ALREADY COMMITTED TO DISK AND COMPILE."
            f" Do NOT copy, repeat, or modify any test from them."
            f" Write ONLY new test functions that cover the uncovered lines in fn_bodies below:\n```rust\n{old_block}\n```\n"
        )
        if len(s) <= remaining:
            old_block_section = s
            remaining -= len(s)
        else:
            dropped.append("old_block")

    # --- Sibling source files (priority 3, planner relevance order) ---
    # Moved before integration tests: wrong type construction causes more failures than
    # wrong test style. Planner-ranked siblings (e.g. error.rs overlap 3-4) must fit.
    sib_parts: list[str] = []
    for (srel, scontent) in sibling_files:  # order from _select_relevant_siblings (most relevant first)
        s = f"\n// --- {srel} ---\n{scontent}"
        if len(s) <= remaining:
            sib_parts.append(s)
            remaining -= len(s)
        else:
            dropped.append(f"sibling:{srel}")
    sib_section = (
        "\nSibling source files in this module (type definitions and signatures):\n"
        "```rust\n" + "\n\n".join(sib_parts) + "\n```"
    ) if sib_parts else ""

    # --- Integration tests (priority 4, shortest first) ---
    integ_parts: list[str] = []
    for (irel, icontent) in sorted(integration_tests, key=lambda x: len(x[1])):
        s = f'\n<TESTFILE path="{irel}">\n{icontent}\n</TESTFILE>\n'
        if len(s) <= remaining:
            integ_parts.append(s)
            remaining -= len(s)
        else:
            dropped.append(f"integ_test:{irel}")
    integ_section = (
        "\nIntegration test files (already compile and pass — use as pattern reference):\n"
        + "".join(integ_parts)
    ) if integ_parts else ""

    if dropped:
        _guard("task0c/budget", f"{rel_path}: dropped {', '.join(dropped)}")

    return {
        "cargo": cargo_section,
        "full_source": full_source_section,
        "fn_bodies": fn_bodies_section,
        "unexecuted_fns": unexecuted_fns_section,
        "behaviours": behaviours_section,
        "symbols": symbols_section,
        "history": history_section,
        "what_worked": what_worked_section,
        "retrospective": retrospective_section,
        "old_block": old_block_section,
        "strategy": strategy_section,
        "examples": examples_section,
        "context": context_section,
        "siblings": sib_section,
        "integ_tests": integ_section,
    }


@dataclass(frozen=True, slots=True)
class _PreflightCtx:
    """Collaborators for the pre-augmentation harness compile-and-repair phase."""

    log: Callable[[str], None]
    emit: Callable[..., None]
    is_test_file: Callable[[Path, Path], bool]
    ask: Callable[..., str]
    write: Callable[[Path, str], None]
    extract_files: Callable[[str], dict[str, str]]
    subprocess: Any
    build_timeout: int
    fix_thinking_directive: str
    task0c_model: str
    cargo_env: Callable[[], dict[str, str]]
    match_gen_tests_sibling: Callable[[str], Any]
    strip_gen_tests_decl_n: Callable[[str, int], str]
    strip_all_gen_tests_artifacts: Callable[[Path], int]
    is_gen_tests_file: Callable[[str], bool]
    error_signatures_from_output: Callable[[str], frozenset[str]]
    check_stop: Callable[[str], None]


def _run_harness_preflight(
    *,
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    augmentor: Any,
    ctx: _PreflightCtx,
) -> tuple[float, int, bool, str] | None:
    """Ensure the module's test harness compiles before augmentation.

    Two-stage check (lib, then full harness), an LLM repair fix-loop, and the
    targeted→full→git-restore strip escalation ladder. Returns an early-exit
    result tuple when augmentation must be skipped, or ``None`` to proceed.
    Body moved VERBATIM from ``task0c_augment_module``.
    """
    _log = ctx.log
    _emit = ctx.emit
    is_test_file = ctx.is_test_file
    ask = ctx.ask
    write = ctx.write
    extract_files = ctx.extract_files
    subprocess = ctx.subprocess
    BUILD_TIMEOUT = ctx.build_timeout
    _TASK0C_FIX_THINKING_DIRECTIVE = ctx.fix_thinking_directive
    _TASK0C_MODEL = ctx.task0c_model
    _cargo_env = ctx.cargo_env
    _match_gen_tests_sibling = ctx.match_gen_tests_sibling
    _strip_gen_tests_decl_n = ctx.strip_gen_tests_decl_n
    _strip_all_gen_tests_artifacts = ctx.strip_all_gen_tests_artifacts
    _is_gen_tests_file = ctx.is_gen_tests_file
    _error_signatures_from_output = ctx.error_signatures_from_output
    _check_stop = ctx.check_stop

    # Pre-flight: two-stage compile check.
    # Stage 1: library only — if broken, skip (not TASK 0c's job to fix implementation).
    # Stage 2: full test harness — if broken but stage 1 passes, existing test code is
    #   broken and the LLM is asked to fix it before augmentation begins.
    lib_ok, lib_err = augmentor.lib_compiles(project_dir, module_name)
    if not lib_ok:
        # Check whether the failure is caused by a gen_* block we wrote
        # (syntax errors inside #[cfg(test)] blocks still break cargo build --lib).
        # If so, fall through to the harness fix loop which passes the error to the
        # LLM and only strips as a last resort — preserving generated test progress.
        # If the failure is in the real implementation, skip immediately.
        _lib_err_in_gen = any(
            "mod gen_" in lib_err or
            ("gen_tests" in lib_err) or
            (src_file.name in lib_err and
             "mod gen_" in src_file.read_text(encoding="utf-8", errors="ignore"))
            for src_file in _iter_module_rs(module_dir)
            if not is_test_file(src_file, project_dir)
        )
        if not _lib_err_in_gen:
            # Check whether any of the broken files are writable (i.e. we wrote them).
            # If the error is only in write-protected originals, we truly cannot fix it.
            _broken_in_err = [
                src_file for src_file in _iter_module_rs(module_dir)
                if (str(src_file.relative_to(project_dir)) in lib_err
                    or src_file.name in lib_err)
                and not is_test_file(src_file, project_dir)
                and os.access(src_file, os.W_OK)
            ]
            if not _broken_in_err:
                err_snippet = lib_err[:400]
                _log(f"  [{module_name}] Library does not compile in write-protected files — skipping.")
                _log(f"    Error: {err_snippet}")
                return 0.0, 0, False, "harness_broken_in_protected_files"
            _log(f"  [{module_name}] Library compile failure in writable source — routing to harness fix loop.")
        else:
            _log(f"  [{module_name}] Library compile failure in gen_* block — routing to harness fix loop.")

    def _error_signatures(cargo_output: str) -> frozenset[str]:
        """Extract stable error signatures — error code + message, no file/line numbers."""
        return _error_signatures_from_output(cargo_output)

    def _is_protected_test_file(p: Path) -> bool:
        """Original test files we must never rewrite or strip — they are the
        oracle. Pipeline-generated tests (leftover `*.gen_tests_*.rs` siblings
        and `tests/{stem}_gen_{N}.rs` files carrying the writer origin
        marker) are NOT protected: the harness fix loop owns them and may
        rewrite or delete them. An authored file whose name merely parses as
        `{stem}_gen_{N}` stays protected. Filtering them with plain
        is_test_file made the loop a no-op whenever the breakage was in a
        generated file — zero prompts sent, zero strips — observed live on
        the nightly cluster."""
        return is_test_file(p, project_dir) and not is_claimed_pipeline_gen_test_path(p)

    harness_ok, compile_err_out = augmentor.test_harness_compiles(project_dir, module_name)
    if not harness_ok:
        # FixLoop port: the LLM repair attempts run on the shared driver
        # (the old hand-rolled `while True` never terminated when error
        # signatures kept CHANGING without resolving — floor-stall + the
        # backstop now bound it). The strip ladder runs once on exhaustion,
        # exactly where the old loop escalated on identical signatures.
        compile_err = compile_err_out
        prev_signatures: frozenset[str] = frozenset()
        broken_files: list[Path] = []
        harness_unidentified = False

        def _attempt_harness_fix(fix_attempt: int) -> FixOutcome:
            nonlocal compile_err, prev_signatures, broken_files, harness_unidentified
            _log(f"  [{module_name}] Harness fix attempt {fix_attempt} — sending compile error to LLM.")

            # Find source files mentioned in the error that belong to this module.
            broken_files = []
            for src_file in _iter_module_rs(module_dir):
                rel = str(src_file.relative_to(project_dir))
                if rel in compile_err or src_file.name in compile_err:
                    broken_files.append(src_file)
            if not broken_files:
                for src_file in _iter_module_rs(module_dir):
                    try:
                        if "#[cfg(test)]" in src_file.read_text(encoding="utf-8"):
                            broken_files.append(src_file)
                    except Exception:
                        pass

            if not broken_files:
                _log(f"  [{module_name}] Cannot identify broken file — skipping augmentation.")
                harness_unidentified = True
                return FixOutcome("stop")

            # Pass 1: ask LLM to fix each broken file — preserve as much generated
            # test code as possible rather than discarding it immediately.
            # gen_tests siblings are fixable here; only original tests are not.
            _files_prompted = 0
            for src_file in broken_files:
                if _is_protected_test_file(src_file):
                    continue
                try:
                    current_content = src_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                rel = str(src_file.relative_to(project_dir))

                prompt = f"""A Rust source file has a test block that fails to compile.
{_TASK0C_FIX_THINKING_DIRECTIVE}File: {rel}

Compile error:
```
{compile_err}
```

Current file content:
{current_content}

Fix the test code so it compiles. Common issues to fix:
- Remove or correct calls to non-existent methods or types.
- Fix type inference errors (e.g. replace `assert_eq!(x, [])` with `assert!(x.is_empty())`).
- Remove `#[test]` from helper functions that take arguments.
- Remove imports of crates not listed in Cargo.toml.

Output the complete corrected file.

<FILE path="{rel}">
corrected content here
</FILE>
"""
                response = ask(prompt, model=_TASK0C_MODEL)
                _files_prompted += 1
                files = extract_files(response)
                if rel in files:
                    write(project_dir / rel, files[rel])
                    _log(f"    LLM fixed test code in {rel}")
                else:
                    _log(f"    LLM produced no output for {rel}")

            # Intent-vs-effect record: the attempt was CLAIMED above ("sending
            # compile error to LLM") — record how many prompts actually went
            # out so the auditor (C8) can flag a no-op loop (observed live:
            # 11 claimed attempts, 0 prompts — every broken file was filtered).
            _emit("task0c.harness_fix.attempt", data={
                "module": module_name, "attempt": fix_attempt,
                "broken_files": len(broken_files),
                "files_prompted": _files_prompted,
            })

            ok, out = augmentor.test_harness_compiles(project_dir, module_name)
            if ok:
                _log(f"  [{module_name}] Harness fixed by LLM after {fix_attempt} attempt(s).")
                return FixOutcome("accepted")

            new_signatures = _error_signatures(out)
            resolved = prev_signatures - new_signatures
            _log(f"  [{module_name}] Still broken — {len(new_signatures)} error signature(s), "
                 f"{len(resolved)} resolved since last attempt.")
            prev_signatures = new_signatures
            compile_err = out
            return FixOutcome("failed", err_sig=new_signatures,
                              metric=FixProgress(max(1, len(new_signatures)), 0))

        _harness_fixed, _hf_attempts = drive_fix_loop(
            _attempt_harness_fix,
            # Same convention as every other loop: floor-stall is the real
            # terminator, the hard cap is a runaway backstop.
            hard_cap=int(os.getenv("TASK0C_HARNESS_HARD_CAP", "10000")),
            max_stalls=int(os.getenv("TASK0C_HARNESS_MAX_STALLS", "3")),
            stop_context=f"task0c harness fix {module_name}",
            check_stop=_check_stop,
            on_repeated=lambda _n, _g: _log(
                f"  [{module_name}] No error improvement — targeted strip of broken gen_* block(s)."),
            on_stall=lambda _n, _g: _log(
                f"  [{module_name}] Harness error floor stalled at {_g.best} for "
                f"{_g.stalls} attempt(s) — escalating to targeted strip."),
        )
        if not _harness_fixed:
            if harness_unidentified:
                return 0.0, 0, False, "harness_broken_file_unidentified"
            # Escalation ladder (unchanged from the hand-rolled loop): targeted
            # strip → full strip → git-restore probe; operates on the LAST
            # attempt's broken_files + compile error.
            compile_err_out = compile_err

            def _strip_broken_gen_blocks(src_file: Path, error_text: str) -> bool:
                """Remove gen_tests artifacts that cover broken code. Returns True if changed.

                With the sibling-file design, each `foo.gen_tests_N.rs` IS one block.
                If src_file is a sibling, delete it and remove its parent's decl.
                If src_file is a `tests/{stem}_gen_N.rs` integration file, unlink
                it (there is no parent decl). If src_file is a real source file,
                strip all its gen_tests artifacts.
                """
                sibling_match = _match_gen_tests_sibling(src_file.name)
                if sibling_match:
                    parent_stem, pass_n = sibling_match
                    parent = src_file.with_name(f"{parent_stem}.rs")
                    try:
                        src_file.unlink()
                    except OSError:
                        return False
                    if parent.exists():
                        ptxt = parent.read_text(encoding="utf-8")
                        cleaned = _strip_gen_tests_decl_n(ptxt, pass_n)
                        if cleaned != ptxt:
                            parent.write_text(cleaned, encoding="utf-8")
                    _log(f"  [{module_name}] Deleted broken sibling "
                         f"{src_file.relative_to(project_dir)} (gen_tests_{pass_n}).")
                    return True
                integ = parse_gen_tests_integration_filename(src_file.name)
                if integ is not None and is_claimed_pipeline_gen_test_path(src_file):
                    try:
                        src_file.unlink()
                    except OSError:
                        return False
                    _log(f"  [{module_name}] Deleted broken generated test "
                         f"{src_file.relative_to(project_dir)}.")
                    return True
                # Real source file — strip all gen_tests artifacts.
                n = _strip_all_gen_tests_artifacts(src_file)
                if n > 0:
                    _log(f"  [{module_name}] Stripped {n} gen_tests sibling(s) for "
                         f"{src_file.relative_to(project_dir)}.")
                    return True
                return False

            any_stripped = False
            for src_file in broken_files:
                if _is_protected_test_file(src_file):
                    continue
                if _strip_broken_gen_blocks(src_file, compile_err_out):
                    any_stripped = True

            harness_ok, _broken_err = augmentor.test_harness_compiles(project_dir, module_name)
            if not harness_ok and any_stripped:
                # Targeted strip removed some blocks but harness is still broken.
                # Fall through to a full strip of ALL gen_tests artifacts from broken files.
                _log(f"  [{module_name}] Targeted strip insufficient — doing full gen_tests strip.")
                for src_file in broken_files:
                    if _is_protected_test_file(src_file):
                        continue
                    # Skip gen_tests siblings (already deleted in targeted pass).
                    if _is_gen_tests_file(src_file.name):
                        continue
                    try:
                        n_removed = _strip_all_gen_tests_artifacts(src_file)
                        if n_removed > 0:
                            _log(f"    Full strip: {src_file.relative_to(project_dir)} "
                                 f"({n_removed} sibling(s))")
                    except Exception:
                        pass
                harness_ok, _broken_err = augmentor.test_harness_compiles(project_dir, module_name)
            if not harness_ok:
                # Check if even `cargo build` (no tests) fails — means LLM damaged source.
                _lib_only = subprocess.run(
                    ["cargo", "build", "-p", module_name],
                    cwd=project_dir, capture_output=True, text=True,
                    timeout=BUILD_TIMEOUT, env=_cargo_env(),
                )
                if _lib_only.returncode != 0:
                    _log(f"  [{module_name}] Source files damaged by LLM fix attempts — restoring from git HEAD.")
                    # Restore non-test source files from git HEAD (not test files — those we own).
                    _restore_ok = True
                    for _sf in _iter_module_rs(module_dir):
                        if is_test_file(_sf, project_dir):
                            continue
                        _rel = str(_sf.relative_to(project_dir))
                        _r = subprocess.run(
                            ["git", "checkout", "HEAD", "--", _rel],
                            cwd=project_dir, capture_output=True, text=True,
                        )
                        if _r.returncode != 0:
                            _log(f"    git restore failed for {_rel}: {_r.stderr[:100]}")
                            _restore_ok = False
                    if _restore_ok:
                        harness_ok, _ = augmentor.test_harness_compiles(project_dir, module_name)
                        if harness_ok:
                            _log(f"  [{module_name}] Source restored — harness now compiles.")
                if not harness_ok:
                    _log(f"  [{module_name}] Harness still broken after targeted strip — skipping augmentation.")
                    return 0.0, 0, False, "harness_broken_after_strip"
            else:
                _log(f"  [{module_name}] Harness fixed by targeted strip.")

    return None


@dataclass
class Task0cFileHistory:
    """Per-file stall/retrospective history accumulated across augmentation
    attempts. Mutated in place by the Phase C generator."""

    file_stall_counts: dict[str, int] = field(default_factory=dict)
    file_best_uncov: dict[str, int] = field(default_factory=dict)
    file_prev_uncov_lines: dict[str, set[int]] = field(default_factory=dict)
    skipped_files: set[str] = field(default_factory=set)
    file_what_worked: dict[str, str] = field(default_factory=dict)
    file_retrospectives: dict[str, str] = field(default_factory=dict)
    file_last_qcov: dict[str, int] = field(default_factory=dict)
    file_failed_blocks: dict[str, list[str]] = field(default_factory=dict)
    file_giveup_counts: dict[str, int] = field(default_factory=dict)
    file_loops: dict[str, LoopState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _PhaseCCtx:
    """Per-module collaborators + config for the Phase C generate/commit pass."""

    deps: Task0cDeps
    current_cluster: Any
    cluster_label: str
    split_n: int
    allow_line: str
    prelude_block: str
    prelude_names: Any
    file_stall_limit: int
    retro_deep_at_stall: int
    stall_context: bool
    # True for a behaviour pass (`task0c_behaviour_pass`): the "uncovered"
    # lines are the fns the listed behaviours exercise, so the coverage
    # retrospective/pragma machinery (which classifies real gaps) stays off.
    behaviour_pass: bool = False


@dataclass(frozen=True, slots=True)
class _PhaseCOutcome:
    """How the Phase C pass terminated, propagated to the augmentation loop."""

    kind: str                                  # "continue" | "break" | "return"
    at_ceiling: bool = False
    result: tuple[float, int, bool, str] | None = None
    blocks_admitted: int = 0                   # gen_tests blocks that passed every gate
    # Unexecuted functions a test block was generated for this pass — one
    # targeted attempt each (`fn_execution.note_attempt`), whatever the block
    # then did; `fn_outcomes` is that, per site, as (site, outcome) pairs.
    fn_attempted: frozenset[Any] = frozenset()
    fn_outcomes: tuple[tuple[Any, str], ...] = ()
    # The retrospective's classification of the unexecuted functions it named
    # unambiguously, as (site, category) pairs. Evidence for the record.
    fn_verdicts: tuple[tuple[Any, str], ...] = ()


def _run_phase_c(
    *,
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    project_context: str,
    coverage_strategy: str,
    attempt: int,
    coverage: float,
    covered_lines: int,
    files_data: list[PerFileCoverage],
    hist: Task0cFileHistory,
    ctx: _PhaseCCtx,
    fn_targets: tuple[FnTarget, ...] = (),
    proved: tuple[FnTarget, ...] = (),
    spent: tuple[FnTarget, ...] = (),
) -> _PhaseCOutcome:
    """Generate/augment tests for the attempt's uncovered lines, commit each block
    through its compile-fix loop, and update per-file history. Returns the loop
    signal (continue / break[/at-ceiling] / early-return). Body moved VERBATIM from
    ``task0c_augment_module``; the three outer-loop exits
    become :class:`_PhaseCOutcome` returns.

    `fn_targets` are the functions the cluster compiles that no test executes
    (`FnCoverageRecord.targets`, cluster-gated first). Their files go to the
    front of the queue, each prompt lists their bodies as never executed, and a
    block written for a file charges one attempt to each target its prompt
    carried (`_PhaseCOutcome.fn_attempted`), with what the block did as the
    reason it did or did not reach them (`fn_outcomes`).

    `proved` are the functions the compiler proved no test can execute; their
    uncovered lines are filtered like a cfg-compiled-out line, never aimed at.
    `spent` are the unexecuted functions whose targeted-attempt budget is spent;
    their lines are filtered too, since the stop already reads them accounted
    and a prompt aimed at them would buy the function a second budget.
    """
    deps = ctx.deps
    BUILD_TIMEOUT = deps.BUILD_TIMEOUT
    _COVERAGE_TARGET = deps.COVERAGE_TARGET
    TASK0C_PROMPT_CHAR_CAP = deps.TASK0C_PROMPT_CHAR_CAP
    _FIX_ERR_CAP = deps.FIX_ERR_CAP
    _SIBLING_CAP = deps.SIBLING_CAP
    _SRC_SNIP_CAP = deps.SRC_SNIP_CAP
    _TASK0C_FIX_THINKING_DIRECTIVE = deps.TASK0C_FIX_THINKING_DIRECTIVE
    _TASK0C_MODEL = deps.TASK0C_MODEL
    _TASK0C_THINKING_DIRECTIVE = deps.TASK0C_THINKING_DIRECTIVE
    _WORKING_CAP = deps.WORKING_CAP
    _annotate_uncovered = deps.annotate_uncovered
    _block_is_prose = deps.block_is_prose
    _build_gen_tests_decl = deps.build_gen_tests_decl
    _build_task0c_prompt_sections = functools.partial(_engine_build_prompt_sections, deps)
    _cargo_env = deps.cargo_env
    _world_gate = deps.world_gate
    _settle_test_gate = deps.settle_test_gate
    _condense_rust_to_signatures = deps.condense_rust_to_signatures
    _count_test_attrs = deps.count_test_attrs
    _describe_test_environment = deps.describe_test_environment
    _error_signatures_from_output = deps.error_signatures_from_output
    _extract_branch_guard = deps.extract_branch_guard
    _extract_gen_tests_body = deps.extract_gen_tests_body
    _extract_public_symbols = deps.extract_public_symbols
    _extract_uncovered_functions = deps.extract_uncovered_functions
    _filter_cfg_excluded_lines = deps.filter_cfg_excluded_lines
    _filter_cfg_incompatible_lines = deps.filter_cfg_incompatible_lines
    _flags_unreachable = deps.flags_unreachable
    _gen_tests_path = deps.gen_tests_path
    _get_task0c_history = functools.partial(_engine_get_history, deps)
    _has_nonempty_test_body = deps.has_nonempty_test_body
    _inject_allow_before_any_mod = deps.inject_allow_before_any_mod
    _inject_allow_before_gen_mod = deps.inject_allow_before_gen_mod
    _inject_gen_prelude_imports = deps.inject_gen_prelude_imports
    _is_structural_line = deps.is_structural_line
    _load_file_constraints = deps.load_file_constraints
    _log_task0c_attempt = functools.partial(_engine_log_attempt, deps)
    _next_gen_tests_n = deps.next_gen_tests_n
    _nightly_rules_for_cluster = deps.nightly_rules_for_cluster
    _parse_retrospective_categories = deps.parse_retrospective_categories
    _partition_uncovered_lines = deps.partition_uncovered_lines
    _quick_file_coverage = deps.quick_file_coverage
    _read_all_gen_tests = deps.read_all_gen_tests
    _revert_gen_tests_pass = deps.revert_gen_tests_pass
    _select_relevant_siblings = deps.select_relevant_siblings
    _strip_gen_tests_decls = deps.strip_gen_tests_decls
    _strip_trailing_file_tag = deps.strip_trailing_file_tag
    _check_stop = deps.check_stop
    _task0c_retrospective = functools.partial(_engine_retrospective, deps)
    _task0c_targeted_pragma = functools.partial(_engine_targeted_pragma, deps)
    ask = deps.ask
    extract_test_mod = deps.extract_test_mod
    generate_in_parallel = deps.generate_in_parallel
    is_test_file = deps.is_test_file
    subprocess = deps.subprocess
    _log = log
    _emit = emit
    _current_cluster = ctx.current_cluster
    _cluster_label = ctx.cluster_label
    _behaviour_target = _behaviour_target_of(deps)
    _capture_split = bool(deps.BEHAVIOUR_CAPTURE_SPLIT)
    _suite_specs = _load_behaviour_specs(deps, project_dir, _cluster_label, log=_log)
    _blocks_admitted = 0
    _file_pinned_now: dict[str, frozenset[str]] = {}
    _split_n = ctx.split_n
    _allow_line = ctx.allow_line
    _prelude_block = ctx.prelude_block
    _prelude_names = ctx.prelude_names
    FILE_STALL_LIMIT = ctx.file_stall_limit
    RETRO_DEEP_AT_STALL = ctx.retro_deep_at_stall
    _STALL_CONTEXT = ctx.stall_context
    _log = log
    _emit = emit
    file_stall_counts = hist.file_stall_counts
    file_best_uncov = hist.file_best_uncov
    file_prev_uncov_lines = hist.file_prev_uncov_lines
    skipped_files = hist.skipped_files
    file_what_worked = hist.file_what_worked
    file_retrospectives = hist.file_retrospectives
    file_last_qcov = hist.file_last_qcov
    file_failed_blocks = hist.file_failed_blocks
    file_giveup_counts = hist.file_giveup_counts
    file_loops = hist.file_loops
    # --- Phase C: generate/augment tests for uncovered lines ---
    # Filter to source files in this module with uncovered lines.
    # Skip test files — adding tests inside test files causes circular issues.
    mod_prefix = str(module_dir) + "/"
    fn_targets_by_file: dict[str, list[FnTarget]] = {}
    for _ft in fn_targets:
        if str(project_dir / _ft.site.file).startswith(mod_prefix):
            fn_targets_by_file.setdefault(_ft.site.file, []).append(_ft)
    _proved_lines = {rel: lines_in(t for t in proved if t.site.file == rel)
                     for rel in {t.site.file for t in proved}}
    _spent_lines = {rel: lines_in(t for t in spent if t.site.file == rel)
                    for rel in {t.site.file for t in spent}}
    _fn_attempted: set[Any] = set()
    _fn_outcomes: dict[Any, str] = {}
    _fn_verdicts: dict[Any, str] = {}
    uncovered_by_file: dict[str, tuple[str, set[int]]] = {}
    for fdata in files_data:
        abs_path_str = fdata.abs_path
        if not abs_path_str.startswith(mod_prefix):
            continue
        rel = str(Path(abs_path_str).relative_to(project_dir))
        lines_uncov = {
            t["line"] for t in fdata.traces
            if t["stats"].get("Line", 1) == 0
        }
        _gated = _current_cluster.gated_files if _current_cluster and _current_cluster.gated_files else set()
        if lines_uncov and not is_test_file(Path(abs_path_str), project_dir) \
                and Path(abs_path_str).name != "build.rs" \
                and rel not in _gated:
            uncovered_by_file[rel] = ("", lines_uncov)  # elem [0] is unused (see [1]-only reads)
            # Baseline-floor seed: the file's covered-line count from the
            # start-of-attempt full measurement. The quick-cov accept gate
            # (below) reverts any block whose quick-cov drops at/below this,
            # so a DESTRUCTIVE first block (e.g. a test that aborts the test
            # binary and wipes real coverage — fmt.rs 84→40) is caught instead
            # of sailing through on a None _prev_qcov. Without this seed the
            # gate only guards the 2nd+ block against the 1st, never the 1st
            # against baseline, letting a net-negative block lower total
            # coverage and spin the outer attempt loop.
            file_last_qcov[rel] = fdata.covered
    # A file whose only gap is a function no test enters is a target even when
    # the line reading lists nothing for it (every line pragma'd, or no DA
    # record): the function criterion is measured on FN records, not on lines.
    for _rel, _targets in fn_targets_by_file.items():
        if _rel in uncovered_by_file:
            continue
        uncovered_by_file[_rel] = ("", {_t.first_line for _t in _targets})
        for fdata in files_data:
            if fdata.abs_path == str(project_dir / _rel):
                file_last_qcov[_rel] = fdata.covered

    if not uncovered_by_file:
        _log(f"  [{module_name}] No uncovered lines in module source files.")
        return _PhaseCOutcome("break")

    # Collect per-attempt context once (not per-file) to avoid redundant disk reads.
    # BL-008: Cargo.toml — model must know what dependencies are available.
    cargo_toml_path = module_dir / "Cargo.toml"
    if not cargo_toml_path.exists():
        cargo_toml_path = project_dir / "Cargo.toml"
    cargo_toml_content = ""
    if cargo_toml_path.exists():
        with contextlib.suppress(Exception):
            cargo_toml_content = cargo_toml_path.read_text(encoding="utf-8")

    # BL-004: collect integration test files (tests/*.rs) for pattern reference.
    integration_tests: list[tuple[str, str]] = []
    tests_dir = module_dir / "tests"
    if not tests_dir.exists():
        tests_dir = project_dir / "tests"
    if tests_dir.exists():
        for tf in sorted(tests_dir.rglob("*.rs")):
            try:
                rel = str(tf.relative_to(project_dir))
                content = tf.read_text(encoding="utf-8")
                integration_tests.append((rel, content))
            except Exception:
                pass

    # Sibling source files: all module .rs files except the current target.
    # Include other targeted files too — they define the types the target uses.
    # Only the file being processed is excluded (its content is in the main prompt).
    sibling_files_all: list[tuple[str, str]] = []
    for sib_file in _iter_module_rs(module_dir):
        if is_test_file(sib_file, project_dir):
            continue
        if sib_file.name == "build.rs":
            continue  # build scripts are not library code — no useful type context
        try:
            sib_rel = str(sib_file.relative_to(project_dir))
            sib_text = sib_file.read_text(encoding="utf-8")
            sibling_files_all.append((sib_rel, sib_text))
        except Exception:
            pass

    _file_order_strategy = os.getenv("TASK0C_FILE_ORDER", "easy_first").strip().lower()

    def _fn_rank(rel: str) -> tuple[int, int]:
        # Files carrying an unexecuted function first, cluster-gated ones
        # ahead of shared: a function no test enters is a gap the line queue
        # ranks by size alone, and easy-first put it behind every file with
        # fewer uncovered lines.
        _ts = fn_targets_by_file.get(rel, ())
        return (0 if _ts else 1, 0 if any(_t.cluster_gated for _t in _ts) else 1)

    if _file_order_strategy == "easy_first":
        _ordered_files = sorted(
            uncovered_by_file.items(),
            key=lambda kv: (*_fn_rank(kv[0]), file_stall_counts.get(kv[0], 0), len(kv[1][1]))
        )
    else:
        _ordered_files = sorted(uncovered_by_file.items(), key=lambda kv: _fn_rank(kv[0]))

    _queue_summary = ", ".join(
        f"{rel.split('/')[-1]}({len(unc)}raw{'*' if file_stall_counts.get(rel, 0) > 0 else ''})"
        for rel, (_, unc) in _ordered_files
        if rel not in skipped_files
    )
    _log(f"  [{module_name}] Queue: {len([r for r,_ in _ordered_files if r not in skipped_files])} file(s) — {_queue_summary} (raw=pre-filter uncovered-line count from the coverage backend)"
         + (f"; {len(fn_targets)} unexecuted function(s) targeted first" if fn_targets else ""))
    _emit("task0c.queue", data={"files": [
        [rel.split("/")[-1], len(unc)]
        for rel, (_, unc) in _ordered_files if rel not in skipped_files]})

    _active_files = [r for r, _ in _ordered_files if r not in skipped_files]
    attempt_n_within_outer = 0
    _pass_had_work = False   # set True if any file makes a real LLM call this pass
    # Uncovered lines this pass judged REACHABLE — the post-filter count for every
    # file it looked at, plus the raw count of files it skipped as stalled (those
    # lines are unaddressed, not filtered). This is what "at a ceiling" is a claim
    # about, so it is counted rather than inferred; see the gate at the end of the
    # pass.
    _pass_actionable = 0
    for rel_path, (content, uncov_lines) in _ordered_files:
        attempt_n_within_outer += 1

        if rel_path in skipped_files:
            _log(f"  [{module_name}] Skipping {rel_path} — stalled for {FILE_STALL_LIMIT} consecutive attempts.")
            _pass_actionable += len(uncov_lines)
            continue

        _file_idx = _active_files.index(rel_path) + 1 if rel_path in _active_files else attempt_n_within_outer
        _log(f"  [{module_name}] File {_file_idx}/{len(_active_files)}: {rel_path} ({len(uncov_lines)} raw uncovered lines)")
        _emit("task0c.file", file=rel_path, data={"idx": _file_idx, "total": len(_active_files)})

        abs_path = project_dir / rel_path
        # _raw_text = current on-disk content (including leftover gen_tests decls
        # from a tree that still has src siblings). This is the revert target:
        # on a failed pass we restore _raw_text and unlink the just-written
        # tests/{stem}_gen_N.rs, so previously-committed passes survive intact.
        _raw_text = abs_path.read_text(encoding="utf-8") if abs_path.exists() else content
        # Generated tests live in tests/{stem}_gen_N.rs (and leftover src
        # siblings). Read them all so the LLM sees what it generated before
        # and can extend rather than restart.
        _old_block = _read_all_gen_tests(abs_path)
        # The view shown to the LLM (and used for uncov_lines line counting) strips
        # the path-mod declaration lines so the line numbers match the original src.
        existing_text = _strip_gen_tests_decls(_raw_text).rstrip() + "\n"

        _file_cov_hits: dict[int, int] = {}
        for _fd in files_data:
            if _fd.abs_path == str(abs_path):
                _file_cov_hits = {t["line"]: t["stats"].get("Line", 0) for t in _fd.traces}
                break
        _split = split_uncovered(existing_text, uncov_lines, _file_cov_hits,
                                 _proved_lines.get(rel_path, frozenset()),
                                 cluster=_current_cluster, **_line_filters(deps),
                                 spent_lines=_spent_lines.get(rel_path, frozenset()))
        if _split.artifact:
            _log(f"  [{module_name}] {rel_path} — filtered {len(_split.artifact)} LLVM artifact line(s): {sorted(_split.artifact)}")
        if _split.cfg_incompatible:
            _log(f"  [{module_name}] {rel_path} — filtered {len(_split.cfg_incompatible)} cfg-incompatible line(s) "
                 f"(compiled out under {_current_cluster.name}): {sorted(_split.cfg_incompatible)}")
        if _split.cfg_excluded:
            _log(f"  [{module_name}] {rel_path} — filtered {len(_split.cfg_excluded)} excluded-cfg line(s) "
                 f"(compiled out in {_current_cluster.name}): {sorted(_split.cfg_excluded)}")
        if _split.proved:
            _log(f"  [{module_name}] {rel_path} — filtered {len(_split.proved)} line(s) in functions "
                 f"proved unreachable from tests/: {sorted(_split.proved)}")
        if _split.spent:
            _log(f"  [{module_name}] {rel_path} — filtered {len(_split.spent)} line(s) in functions "
                 f"whose targeted-attempt budget is spent: {sorted(_split.spent)}")
        uncov_lines = set(_split.actionable)

        _file_fn_targets = tuple(fn_targets_by_file.get(rel_path, ()))
        for _ft in _file_fn_targets:
            if not any(_ft.first_line <= _ln <= _ft.last_line for _ln in uncov_lines):
                uncov_lines.add(_ft.first_line)

        if not uncov_lines:
            _log(f"  [{module_name}] {rel_path} — no actionable uncovered lines after filtering, skipping.")
            continue

        _log(f"  [{module_name}] {rel_path} — {len(uncov_lines)} actionable uncovered lines (post-filter)"
             + (f", {len(_file_fn_targets)} function(s) no test executes: "
                + ", ".join(_ft.label for _ft in _file_fn_targets) if _file_fn_targets else ""))
        _pass_actionable += len(uncov_lines)
        _fn_span_lines = {_ln for _ft in _file_fn_targets
                          for _ln in range(_ft.first_line, _ft.last_line + 1)}
        _line_targets = uncov_lines - _fn_span_lines
        fn_bodies = (_extract_uncovered_functions(existing_text, _line_targets)
                     if _line_targets else "")
        _unexecuted_fns = _render_unexecuted_fns(_raw_text, _file_fn_targets)
        pub_symbols = _extract_public_symbols(existing_text)
        # The cluster rules, with the target-scope half rendered from THIS
        # prompt's targets: which listed functions/lines only this cluster's
        # cfg compiles, and which are shared code no test here reaches.
        # A split-and-merge subset shows its own share of both, so the rule is
        # a function of the targets the caller is about to render, never of the
        # file's totals: on run 3044-3c9670bf (2026-09-17) the wrapper.rs subset
        # that listed one never-executed body carried a rule naming five, one of
        # them cfg-gated and absent from the prompt.
        def _scope_rules(_targets: tuple[FnTarget, ...], _lines: set[int], *,
                         existing_text: str = existing_text) -> str:
            _gated_line_n = 0
            if _current_cluster is not None and _lines:
                _gated_line_n = len(flipped_cfg_set(
                    _current_cluster.cfg_true, _current_cluster.cfg_false,
                ).excluded_uncovered_lines(existing_text, _lines))
            return "\n".join(_r for _r in (
                _nightly_rules_for_cluster(),
                target_scope_rule(
                    _current_cluster,
                    gated_fns=[_ft.label for _ft in _targets if _ft.cluster_gated],
                    shared_fns=[_ft.label for _ft in _targets if not _ft.cluster_gated],
                    gated_lines=_gated_line_n,
                    shared_lines=len(_lines) - _gated_line_n)) if _r)

        _cluster_rules = _scope_rules(_file_fn_targets, _line_targets)

        # The behaviours this file has not pinned yet: listed in the prompt and
        # required of the block when the target is on. `already_pinned` is what
        # the file's EXISTING gen_tests blocks pin, so the metric the cluster
        # loop reads (`task0c_behaviour_coverage`) and the list the model sees
        # agree on what is left.
        _file_items = declared_items(existing_text)
        _file_specs = select_behaviours_for_file(
            _suite_specs,
            file_fn_names={it.name for it in _file_items if it.kind == "fn"},
            file_pub_items=public_item_names(_file_items),
            already_pinned=behaviours_pinned_by(_old_block, _suite_specs))
        # The capture route owns the classes whose exact value a model would
        # have to predict, so they are neither listed here nor required of the
        # block. Measured on the live 230-spec suite: 155 of 230 specs are
        # pinnable and almost all of those are EXACT_FORMAT / IDENTITY /
        # BOUNDED_RESOURCE, so this empties `_file_specs` for most files and
        # `require_pin` (which already keys on `bool(_file_specs)`) turns
        # itself off — leaving this task with the public-API refusal and the
        # job it can do, reaching code paths. Nothing is lost by not guessing
        # them: the crate's own tests already assert those values, and the
        # captures add what harvest missed, as tests, without this prompt
        # asking the model to predict a rendering.
        _pre_split = _file_specs
        _file_specs = drop_capture_owned(_file_specs, enabled=_capture_split)
        if len(_file_specs) != len(_pre_split):
            _log(f"  [{module_name}] {rel_path} — {len(_pre_split) - len(_file_specs)} "
                 f"capture-owned behaviour(s) not listed (their exact value is captured, "
                 f"not predicted)")
        _behaviours_section = render_behaviour_list(_file_specs, rel_path=rel_path)
        _policy = AdmissionPolicy(require_pin=_behaviour_target > 0 and bool(_file_specs),
                                  refuse_private=True)
        if _file_specs:
            _log(f"  [{module_name}] {rel_path} — {len(_file_specs)} harvested behaviour(s) not "
                 f"yet pinned (pin {'required' if _policy.require_pin else 'requested'})")

        # BL-002: retrieve prior attempt history for this file.
        attempt_history = _get_task0c_history(project_dir, rel_path)

        # BL-014: Python planner — rank siblings by type-name overlap.
        # Exclude the current target from its own sibling list.
        relevant_siblings = _select_relevant_siblings(
            rel_path, existing_text,
            [(srel, sc) for srel, sc in sibling_files_all if srel != rel_path],
            project_dir
        )
        # Condense sibling content to signatures (max 3k each) so large files like
        # error.rs (35k full) and lib.rs (20k full) actually fit in the context budget.
        # Exceptions: kind.rs and error.rs are never condensed when selected as siblings
        # because their critical mechanisms (autoref specialization, vtable layout) live
        # in function bodies — condensing strips exactly the context the LLM needs.
        _FULL_SOURCE_SIBLINGS = {"kind.rs", "error.rs"}
        relevant_siblings = [
            (srel, sc if Path(srel).name in _FULL_SOURCE_SIBLINGS
                   else _condense_rust_to_signatures(sc, Path(srel).name)[:_SIBLING_CAP])
            for srel, sc in relevant_siblings
        ]

        # Measure prompt scaffold (everything in the prompt template that
        # is NOT a `secs[...]` section) so the budget reflects actual
        # overhead instead of a magic estimate.
        _pass_n = _next_gen_tests_n(abs_path)
        _integ_rel = gen_tests_integration_rel(Path(rel_path).stem, _pass_n)
        _allow_stripped = _allow_line.strip()
        _crate_allow = (
            "#![allow(" + _allow_stripped[len("#[allow("):] + "\n"
            if _allow_stripped.startswith("#[allow(") and _allow_stripped.endswith(")]") else
            (_allow_stripped + "\n" if _allow_stripped else ""))
        _prelude_flat = "\n".join(
            ln.strip() for ln in _prelude_block.splitlines() if ln.strip())
        output_format = (
            f'<FILE path="{_integ_rel}">\n'
            f'{_crate_allow}'
            f'use {module_name}::*;\n'
            f'{_prelude_flat}\n\n'
            f'#[test]\nfn test_example() {{\n    // tests\n}}\n'
            f'</FILE>'
        )
        _placement_rules = _integration_test_rules(
            crate_name=module_name, prelude_names=_prelude_names)
        _coverage_status_probe = _coverage_status_line(
            coverage, line_targets=len(_line_targets),
            fn_targets=_file_fn_targets, whole_file=True)
        if ctx.behaviour_pass:
            _coverage_status_probe = (
                "\nBehaviour pass: the fns in fn_bodies below are the ones the listed harvested "
                "behaviours exercise. Every test must pin one of the listed behaviours by asserting "
                "its exact `then`; line coverage is secondary here.\n")
        _env_section = _describe_test_environment()
        _file_constraints = _load_file_constraints(project_dir)
        _target_basename = Path(rel_path).name
        _file_constraints_section = _file_constraints.get(_target_basename, "")
        _scaffold_template = f"""You are writing Rust unit tests that specify the behavioral contract of source code.
{_TASK0C_THINKING_DIRECTIVE}{_coverage_status_probe}
Target file: {rel_path}
{_env_section}
Write a `tests/` integration-test file for `{rel_path}` at `{_integ_rel}`.
Write a new test block covering the uncovered lines shown in fn_bodies above{_calls_clause(_file_fn_targets)}.

{_TEST_CONTRACT}
{_placement_rules}
{_cluster_rules}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}
{output_format}
"""

        # BL-007: build whole-folder context with prioritized degradation.
        secs = _build_task0c_prompt_sections(
            rel_path=rel_path,
            existing_text=existing_text,
            fn_bodies=fn_bodies,
            pub_symbols=pub_symbols,
            cargo_toml_content=cargo_toml_content,
            integration_tests=integration_tests,
            sibling_files=relevant_siblings,
            coverage_strategy=coverage_strategy,
            attempt_history=attempt_history,
            project_context=project_context,
            old_block=_old_block,
            what_worked=file_what_worked.get(rel_path, ""),
            retrospective=file_retrospectives.get(rel_path, ""),
            uncov_lines=uncov_lines,
            scaffold_chars=len(_scaffold_template),
            behaviours=_behaviours_section,
            unexecuted_fns=_unexecuted_fns,
        )

        # The full _coverage_status may add a dead-code hedge when there
        # are few remaining uncovered lines; the scaffold probe used the
        # short form only (the hedge is best-effort overhead).
        _coverage_status = (
            _coverage_status_probe
            + (
                "IMPORTANT: grcov sometimes misreports dead branches, closing braces, or comments as "
                "uncovered even when the surrounding code executes fine. If after careful analysis "
                "the uncovered line(s) appear to be a dead code path (e.g. an if-branch whose "
                "condition is structurally never true on modern Rust), a comment, or a closing brace "
                "— do NOT attempt to cover them. Instead output an empty test block with a comment "
                "explaining why the line is unreachable. Do not waste tests on genuinely dead code.\n"
                if uncov_lines and len(uncov_lines) <= 3 else ""
            )
        )

        # Meta-awareness (via the shared refine-loop primitive): prepend this file's stall
        # trajectory + latest retro so the model knows it is repeating (it otherwise re-tries
        # blind). The LoopState is recorded after each pass (below).
        _giveup_before = file_giveup_counts.get(rel_path, 0)
        if _STALL_CONTEXT:
            _loop_ctx = file_loops.setdefault(rel_path, LoopState()).context()
            if _loop_ctx:
                _coverage_status = _loop_ctx + _coverage_status

        # B023: bind per-file loop vars at def time (these closures are
        # only called within this iteration today; binding makes that
        # permanent instead of incidental). Same for the three below.
        def _render_prompt(_secs: dict[str, str], _cov_status: str, *,
                           rel_path: str = rel_path, _integ_rel: str = _integ_rel,
                           _env_section: str = _env_section,
                           _file_constraints_section: str = _file_constraints_section,
                           _placement_rules: str = _placement_rules,
                           output_format: str = output_format,
                           _cluster_rules: str = _cluster_rules,
                           _calls: str = _calls_clause(_file_fn_targets)) -> str:
            return f"""You are writing Rust unit tests that specify the behavioral contract of source code.
{_TASK0C_THINKING_DIRECTIVE}{_secs['strategy']}{_secs['history']}{_secs['what_worked']}{_secs['retrospective']}{_secs['context']}{_cov_status}
Target file: {rel_path}
{_env_section}{_secs['cargo']}{_secs['full_source']}{_secs['unexecuted_fns']}{_secs['fn_bodies']}{_secs['behaviours']}{_secs['symbols']}{_secs['examples']}{_secs['integ_tests']}{_secs['siblings']}{_secs['old_block']}
Write a `tests/` integration-test file for `{rel_path}` at `{_integ_rel}`.
{"Write ONLY new test functions for the uncovered lines in fn_bodies above" + _calls + ". The existing test module(s) shown are already on disk — do NOT copy or repeat any test from them." if _secs['old_block'] else "Write a new test block covering the uncovered lines shown in fn_bodies above" + _calls + "."}

{_TEST_CONTRACT}
{_placement_rules}
{_cluster_rules}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}
{output_format}
"""

        prompt = _render_prompt(secs, _coverage_status)

        def _subset_prompt(_subset_lines: set[int], *,
                           rel_path: str = rel_path, existing_text: str = existing_text,
                           pub_symbols: str = pub_symbols,
                           cargo_toml_content: str = cargo_toml_content,
                           integration_tests: Any = integration_tests,
                           relevant_siblings: Any = relevant_siblings,
                           attempt_history: str = attempt_history,
                           _old_block: str = _old_block,
                           _scaffold_template: str = _scaffold_template,
                           _coverage_status_probe: str = _coverage_status_probe,
                           _behaviours_section: str = _behaviours_section,
                           _raw_text: str = _raw_text,
                           _file_fn_targets: tuple[FnTarget, ...] = _file_fn_targets,
                           _fn_span_lines: set[int] = _fn_span_lines,
                           coverage: float = coverage,
                           _behaviour_pass: bool = ctx.behaviour_pass,
                           _scope_rules: Callable[[tuple[FnTarget, ...], set[int]], str]
                           = _scope_rules) -> str:
            """Render a generation prompt targeting only a subset of the
                file's uncovered lines (split-and-merge). Same file context as
                the full prompt; only the fn_bodies/target section narrows, so
                the big prefix is shared across the concurrent subset requests.
                An unexecuted function belongs to the subset holding its lines."""
            _sub_lines = _subset_lines - _fn_span_lines
            _sfn = (_extract_uncovered_functions(existing_text, _sub_lines)
                    if _sub_lines else "")
            _sub_targets = tuple(
                _ft for _ft in _file_fn_targets
                if any(_ft.first_line <= _ln <= _ft.last_line for _ln in _subset_lines))
            # Every claim about what this prompt targets is rendered from
            # `_sub_targets`/`_sub_lines`, the two the sections below render:
            # the status sentence, the cluster scope rule and the call list.
            _sub_status = (_coverage_status_probe if _behaviour_pass else
                           _coverage_status_line(
                               coverage, line_targets=len(_sub_lines),
                               fn_targets=_sub_targets, whole_file=False))
            _ssecs = _build_task0c_prompt_sections(
                rel_path=rel_path,
                existing_text=existing_text,
                fn_bodies=_sfn,
                pub_symbols=pub_symbols,
                cargo_toml_content=cargo_toml_content,
                integration_tests=integration_tests,
                sibling_files=relevant_siblings,
                coverage_strategy=coverage_strategy,
                attempt_history=attempt_history,
                project_context=project_context,
                old_block=_old_block,
                what_worked=file_what_worked.get(rel_path, ""),
                retrospective=file_retrospectives.get(rel_path, ""),
                uncov_lines=_subset_lines,
                scaffold_chars=(len(_scaffold_template)
                                - len(_coverage_status_probe) + len(_sub_status)),
                behaviours=_behaviours_section,
                unexecuted_fns=_render_unexecuted_fns(_raw_text, _sub_targets),
            )
            return _render_prompt(_ssecs, _sub_status,
                                  _cluster_rules=_scope_rules(_sub_targets, _sub_lines),
                                  _calls=_calls_clause(_sub_targets))

        # Retry generation — thinking mode sometimes returns empty output.
        # On retry, send a short explicit follow-up instead of the full prompt again:
        # repeating the same large prompt produces the same empty result.
        def _save_stream_log(gen_attempt: int | str, query: str, resp: str, reason: str, *,
                             rel_path: str = rel_path, attempt: int = attempt) -> None:
            _fname = rel_path.replace("/", "_").replace(".", "_")
            _path = project_dir / STREAM_LOG_DIR / f"stream_loop_{attempt}_{_fname}_gen{gen_attempt}.log"
            try:
                _path.parent.mkdir(parents=True, exist_ok=True)
                _path.write_text(
                    f"# reason: {reason}\n"
                    f"# file: {rel_path}  outer_attempt: {attempt}  gen_attempt: {gen_attempt}\n"
                    f"# prompt_chars: {len(query)}\n"
                    f"{'='*60}\n## PROMPT\n{'='*60}\n{query}\n"
                    f"{'='*60}\n## RESPONSE\n{'='*60}\n{resp}\n",
                    encoding="utf-8",
                )
                _log(f"  [{module_name}] Stream saved → {_path}")
            except Exception as _e:
                _log(f"  [{module_name}] Could not save stream log: {_e}")

        def _finalize_and_commit(raw_block: str, pass_n: int, base_text: str, *,
                                 rel_path: str = rel_path, abs_path: Path = abs_path,
                                 attempt: int = attempt, uncov_lines: set[int] = uncov_lines,
                                 existing_text: str = existing_text,
                                 secs: dict[str, str] = secs,
                                 _file_constraints_section: str = _file_constraints_section,
                                 _file_specs: tuple[Any, ...] = _file_specs,
                                 _policy: AdmissionPolicy = _policy,
                                 _behaviours_section: str = _behaviours_section,
                                 _placement_rules: str = _placement_rules,
                                 _cluster_rules: str = _cluster_rules,
                                 ) -> tuple[str, str]:
            """Inject allow-line + prelude into a raw gen_tests block, reject
                blocks with no #[test] fns, then write `tests/{stem}_gen_{pass_n}.rs`
                as an integration test (no path-mod in the source) and run the
                compile-fix loop. Returns (status, final_block), status ∈
                {"committed","no_tests","failed"}. On failure restores
                `base_text` and unlinks the written test — so in split-and-merge
                any subset files already committed before this one survive."""
            nonlocal _pass_had_work
            block = raw_block
            block = _inject_allow_before_gen_mod(block, _allow_line)
            block = _inject_gen_prelude_imports(block)
            if _count_test_attrs(block) == 0:
                _log(f"  [{module_name}] {rel_path} — LLM block has 0 #[test] functions; "
                     f"skipping write (no productive content).")
                _log_task0c_attempt(project_dir, rel_path, attempt, block, "", "no_tests_in_block")
                return "no_tests", block
            _sib = project_dir / gen_tests_integration_rel(abs_path.stem, pass_n)
            _test_bin = _sib.stem
            # Its generating world's gate until the block is accepted: nothing
            # has shown it true in another world yet. The measured gate
            # replaces it at the commit below.
            _gate = _world_gate(_current_cluster) if _current_cluster else ""
            # FixLoop port: verify→LLM-fix cycle on the shared driver.
            # The old hand-rolled loop terminated only on identical error
            # signatures — floor-stall + the backstop now also bound the
            # "signatures keep changing but never resolve" case.
            # `err_text` carries whichever gate rejected the block last — compile
            # diagnostics or its own tests' panic output; `runtime` says which.
            _fc: dict[str, Any] = {"err_text": "", "sigs": frozenset(),
                                   "reverted": False, "runtime": False,
                                   "contract": False, "contract_refusals": 0}

            def _attempt_block_fix(_fix_n: int) -> FixOutcome:
                nonlocal block, _pass_had_work, _blocks_admitted
                _err_text = _fc["err_text"]
                _runtime = bool(_fc["runtime"])
                _contract = bool(_fc["contract"])
                if _fix_n > 1:
                    _what = ("Contract refusal" if _contract
                             else "Failing own test(s)" if _runtime else "Compile error")
                    _log(f"  [{module_name}] {_what} in {rel_path} (fix attempt {_fix_n - 1}) "
                         f"— sending to LLM.")
                    if not _contract:
                        _emit("task0c.gen_tests_unsatisfiable" if _runtime else "task0c.compile_error",
                              file=rel_path)
                    _fix_history = _get_task0c_history(project_dir, rel_path)
                    _fix_history_section = (f"\n{_fix_history}\n") if _fix_history.strip() else ""
                    _err_capped = _err_text[-3_000:] if len(_err_text) > 3_000 else _err_text
                    _fix_extra = len(_err_capped) + len(block) + len(_fix_history_section) + 4000
                    _fix_core = (secs['cargo'] + secs['full_source'] + secs['unexecuted_fns']
                                 + secs['fn_bodies'] + secs['behaviours'] + secs['symbols'])
                    _fix_optional = secs['integ_tests'] + secs['siblings']
                    _FIX_CHAR_LIMIT = TASK0C_PROMPT_CHAR_CAP
                    _fix_ctx = _fix_core + (_fix_optional if len(_fix_core) + len(_fix_optional) + _fix_extra < _FIX_CHAR_LIMIT else "")
                    _fix_head = (_CONTRACT_FIX_HEAD if _contract
                                 else _RUNTIME_FIX_HEAD if _runtime
                                 else _COMPILE_FIX_HEAD).format(
                        rel_path=rel_path, directive=_TASK0C_FIX_THINKING_DIRECTIVE,
                        history=_fix_history_section, err=_err_capped)
                    fix_prompt = f"""{_fix_head}
    Current test block (fix this):
    {block}

    --- Full context below — same constraints apply as when you generated the block ---
    {_fix_ctx}
    {_placement_rules}
    {_TEST_CONTRACT}
    {_cluster_rules}
    See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
    project-specific import paths, type construction patterns, and hard constraints.
    {_file_constraints_section}"""
                    fix_response = ask(fix_prompt, model=_TASK0C_MODEL, think=None)
                    fixed = extract_test_mod(fix_response)
                    if fixed:
                        _pre_fix = block
                        block = _strip_trailing_file_tag(fixed)
                        block = _inject_allow_before_gen_mod(block, _allow_line)
                        _weak = weakening_reason(_pre_fix, block)
                        if _weak:
                            _log(f"  [{module_name}] fix would silence rather than correct "
                                 f"({_weak}) — reverting {rel_path}.")
                            _revert_gen_tests_pass(abs_path, base_text, _sib)
                            _fc["reverted"] = True
                            return FixOutcome("stop")
                    else:
                        _log(f"  [{module_name}] LLM returned no fix — reverting {rel_path}.")
                        _revert_gen_tests_pass(abs_path, base_text, _sib)
                        _fc["reverted"] = True
                        return FixOutcome("stop")
                _sib.parent.mkdir(parents=True, exist_ok=True)
                _integ = as_integration_test(
                    block, crate=module_name, allow_line=_allow_line)
                if _gate:
                    _integ = f"#![cfg({_gate})]\n" + _integ
                _sib.write_text(_integ, encoding="utf-8")
                check = subprocess.run(
                    ["cargo", "test", "--no-run", "-p", module_name],
                    cwd=project_dir, capture_output=True, text=True,
                    timeout=BUILD_TIMEOUT, env=_cargo_env(),
                )
                if check.returncode == 0:
                    # Compiling is not evidence that an expectation is true: run the
                    # block's own tests against the implementation they specify, or a
                    # wrong assertion becomes an oracle entry no fill can ever satisfy.
                    _adm = _engine_runtime_gate(
                        project_dir=project_dir, module_name=module_name,
                        test_bin=_test_bin,
                        block=block, subprocess=subprocess, cargo_env=_cargo_env,
                        timeout=BUILD_TIMEOUT)
                    if not _adm.admitted:
                        _adm_detail = _adm.detail(_FIX_ERR_CAP)
                        _log(f"  [{module_name}] gen_tests_{pass_n} in {rel_path} compiles but "
                             f"{len(_adm.failed_names)} of its own test(s) fail on the code they "
                             f"specify — the expectation is wrong, not the code: "
                             f"{', '.join(sorted(_adm.failed_names))}")
                        _emit("task0c.gen_tests_unsatisfiable", file=rel_path,
                              data={"pass_n": pass_n, "tests": sorted(_adm.failed_names)})
                        _log_task0c_attempt(project_dir, rel_path, attempt + _fix_n - 1,
                                            block, _adm_detail, "test_failure")
                        _fc["err_text"] = _adm_detail
                        _fc["runtime"] = True
                        _fc["contract"] = False
                        _fc["sigs"] = _adm.failed_names
                        # The harness compiled and RAN — a runtime test failure is a
                        # test-tier metric, never comparable to a compile-error
                        # count on the same gauge.
                        return FixOutcome("failed", err_sig=_adm.failed_names,
                                          metric=FixProgress(0, max(1, len(_adm.failed_names))))
                    # Passing on the on-disk implementation is necessary, not
                    # sufficient: the block must also pin a listed behaviour
                    # EXACTLY and stay on the public API. A refusal re-prompts
                    # once with the reason (and the list again, for the pin
                    # case); a second refusal of the same file/pass reverts.
                    _cv = judge_block_contract(block, specs=_file_specs,
                                               file_source=existing_text, policy=_policy)
                    _reason = _cv.refused_reason
                    if _reason is not None:
                        _fc["contract_refusals"] = int(_fc["contract_refusals"]) + 1
                        _final = int(_fc["contract_refusals"]) >= 2
                        _log(f"  [{module_name}] gen_tests_{pass_n} in {rel_path} admitted by the "
                             f"oracle but REFUSED by contract: {_reason}")
                        _emit("task0c.gen_tests_contract_refused", file=rel_path,
                              data={"pass_n": pass_n, "reason": _reason,
                                    "private": list(_cv.private), "pinned": sorted(_cv.pinned),
                                    "stop": _cv.stop_word if _final else ""})
                        _log_task0c_attempt(project_dir, rel_path, attempt + _fix_n - 1,
                                            block, _reason, "contract_refused")
                        if _final:
                            _log(f"  [{module_name}] {_cv.stop_word}: gen_tests_{pass_n} in "
                                 f"{rel_path} refused by contract twice — reverting.")
                            _revert_gen_tests_pass(abs_path, base_text, _sib)
                            _fc["reverted"] = True
                            return FixOutcome("stop")
                        _fc["err_text"] = _reason + (
                            "\n" + _behaviours_section
                            if (not _cv.private and _behaviours_section) else "")
                        _fc["runtime"] = False
                        _fc["contract"] = True
                        _fc["sigs"] = frozenset({"contract:" + _reason})
                        return FixOutcome("failed", err_sig=frozenset({"contract:" + _reason}),
                                          metric=FixProgress(0, 1))
                    # The last judge, and the one the suite is built on: the
                    # block's worlds, its own first. It asks more than the
                    # admission above on one point — every declared test must
                    # be REPORTED passing, so an ignored test or a test the
                    # block compiles out is not a pass. A block it refuses is
                    # a failed attempt like any other: the fix loop is told
                    # why, and nothing below counts it as admitted.
                    if _settle_test_gate(project_dir, module_name, _sib, block,
                                         _current_cluster) is None:
                        _why = (
                            "The file compiles and no test failed, but not every #[test] in "
                            "it was reported PASSING when run in the build it was written "
                            "for. Every test must run and pass: no `#[ignore]`, and no "
                            "`#[cfg(...)]` (on a test or at the top of the file) that "
                            "compiles a test out.")
                        _log(f"  [{module_name}] gen_tests_{pass_n} in {rel_path} is not "
                             "reported passing in the world that generated it.")
                        _log_task0c_attempt(project_dir, rel_path, attempt + _fix_n - 1,
                                            block, _why, "test_failure")
                        _fc["err_text"] = _why
                        _fc["runtime"] = True
                        _fc["contract"] = False
                        _fc["sigs"] = frozenset({"no_world"})
                        return FixOutcome("failed", err_sig=frozenset({"no_world"}),
                                          metric=FixProgress(0, 1))
                    _log_task0c_attempt(project_dir, rel_path, attempt + _fix_n - 1, block, "", "success")
                    _pins = (", pins " + ", ".join(sorted(_cv.pinned))) if _cv.pinned else ""
                    _log(f"  [{module_name}] Appended gen_tests_{pass_n} to {rel_path} "
                         f"({len(uncov_lines)} uncovered lines targeted{_pins})")
                    _emit("task0c.gen_tests_appended", file=rel_path,
                          data={"pass_n": pass_n, "uncovered": len(uncov_lines)})
                    _emit("task0c.behaviours_pinned", file=rel_path,
                          data={"pass_n": pass_n, "ids": sorted(_cv.pinned)})
                    _file_pinned_now[rel_path] = _cv.pinned
                    _blocks_admitted += 1
                    _pass_had_work = True
                    return FixOutcome("accepted")
                compile_err = (check.stdout + check.stderr).strip()
                cur_sigs = _error_signatures_from_output(compile_err)
                if "unclosed delimiter" in compile_err or "mismatched closing delimiter" in compile_err:
                    _log(f"  [{module_name}] Unclosed delimiter — reverting {rel_path}.")
                    _revert_gen_tests_pass(abs_path, base_text, _sib)
                    _fc["reverted"] = True
                    return FixOutcome("stop")
                if _fix_n > 1:
                    resolved = _fc["sigs"] - cur_sigs
                    _log(f"  [{module_name}] {len(resolved)} error(s) resolved in {rel_path}, "
                         f"{len(cur_sigs)} remaining — continuing.")
                _log_task0c_attempt(project_dir, rel_path, attempt + _fix_n - 1, block, compile_err, "compile_error")
                _fc["err_text"] = compile_err
                _fc["runtime"] = False
                _fc["contract"] = False
                _fc["sigs"] = cur_sigs
                return FixOutcome("failed", err_sig=cur_sigs,
                                  metric=FixProgress(max(1, len(cur_sigs)), 0))

            _committed, _ = drive_fix_loop(
                _attempt_block_fix,
                hard_cap=int(os.getenv("TASK0C_BLOCK_FIX_HARD_CAP", "10000")),
                max_stalls=int(os.getenv("TASK0C_BLOCK_FIX_MAX_STALLS", "3")),
                stop_context=f"task0c gen_tests fix {rel_path}",
                check_stop=_check_stop,
                on_repeated=lambda _rn, _rg: _log(
                    f"  [{module_name}] No error improvement in {rel_path} — reverting."),
                on_stall=lambda _sn, _sg: _log(
                    f"  [{module_name}] Error floor stalled in {rel_path} at {_sg.best} "
                    f"for {_sg.stalls} attempt(s) — reverting."),
            )
            if _committed:
                return "committed", block
            if not _fc["reverted"]:
                _revert_gen_tests_pass(abs_path, base_text, _sib)
            return "failed", block

        block = ""
        _block_committed = False
        # Lines whose test block compiled and ran this pass — the evidence an
        # unexecuted function's targeted attempt is charged on.
        _ran_lines: set[int] = set()
        # Lines whose prompt got a test block back — the evidence an
        # unexecuted function was aimed at, whether or not the block compiled.
        _aimed_lines: set[int] = set()
        _did_split = False
        _sibling_path = project_dir / gen_tests_integration_rel(abs_path.stem, _pass_n)
        # Split-and-merge: partition the file's uncovered functions into N
        # disjoint subsets, generate a test block for each CONCURRENTLY
        # (shared-prefix prompts batch on the GPU), then commit each through
        # the normal compile-fix loop so survivors accumulate (additive).
        # Coverage (quick-cov below) runs once on the merged result.
        if _split_n > 1:
            _partitions = _partition_uncovered_lines(existing_text, uncov_lines, _split_n)
            if len(_partitions) >= 2:
                _did_split = True
                _queries = [_subset_prompt(_p) for _p in _partitions]
                _responses = generate_in_parallel(_queries, model=_TASK0C_MODEL, think=None)
                _pass_had_work = True  # LLM ran; empty extract is not "all files filtered"
                _raw_subs: list[str] = []
                _raw_sub_lines: list[set[int]] = []
                for _i, _resp in enumerate(_responses):
                    _raw_sub = extract_test_mod(_resp)
                    if _raw_sub:
                        _raw_sub = _strip_trailing_file_tag(_raw_sub)
                    if _raw_sub:
                        _raw_subs.append(_raw_sub)
                        _raw_sub_lines.append(set(_partitions[_i]))
                        _aimed_lines |= set(_partitions[_i])
                        continue
                    # A dropped subset reply is the crime scene for a routing
                    # defect: without the raw prefix and the stream log, an
                    # extractor that silently returns "" is indistinguishable
                    # from a model that answered nothing.
                    _snippet = _resp[:300].replace("\n", " ") if _resp else "(no output)"
                    _log(f"  [{module_name}] Empty generation for {rel_path} "
                         f"(split subset {_i + 1}/{len(_responses)}). Raw: {_snippet}")
                    _save_stream_log(f"sub{_i}", _queries[_i], _resp or "",
                                     "empty_generation_split")
                if not _raw_subs:
                    # Split spent the GPU and extracted nothing — do not skip
                    # the single-shot path that would retry the same replies.
                    _did_split = False
                    _log(f"  [{module_name}] split-and-merge {rel_path}: no block "
                         f"extracted from {len(_responses)} subset reply(ies) — "
                         f"falling back to single-shot.")
                # Combine the subset blocks into ONE sibling per attempt when
                # their top-level item names are disjoint; only a genuine
                # cross-subset name clash falls back to one-sibling-per-subset.
                _merged = merge_gen_tests_blocks(_raw_subs) if _raw_subs else None
                if _merged is not None:
                    _pn = _next_gen_tests_n(abs_path)
                    _st, _ = _finalize_and_commit(_merged, _pn, _raw_text)
                    _block_committed = _st == "committed"
                    if _block_committed:
                        _sibling_path = project_dir / gen_tests_integration_rel(abs_path.stem, _pn)
                        for _sl in _raw_sub_lines:
                            _ran_lines |= _sl
                    _log(f"  [{module_name}] split-and-merge {rel_path}: "
                         f"{len(_raw_subs)} subset block(s) → 1 sibling ({_st})")
                # Per-subset fallback: name clash (no merge) OR the merged block
                # failed compile-fix (file reverted → all subsets lost). Re-commit
                # subsets individually so survivors accumulate (never drop
                # a passing test). Skip subsets already absorbed by a committed merge.
                if _raw_subs and (_merged is None or not _block_committed):
                    _reason = "name clash" if _merged is None else "merge failed"
                    _base = abs_path.read_text(encoding="utf-8")
                    _n_ok = 0
                    for _raw_sub, _sl in zip(_raw_subs, _raw_sub_lines, strict=True):
                        _pn = _next_gen_tests_n(abs_path)
                        _st, _ = _finalize_and_commit(_raw_sub, _pn, _base)
                        if _st == "committed":
                            _ran_lines |= _sl
                            _base = abs_path.read_text(encoding="utf-8")
                            _sibling_path = project_dir / gen_tests_integration_rel(abs_path.stem, _pn)
                            _n_ok += 1
                    _block_committed = _n_ok > 0
                    _log(f"  [{module_name}] split-and-merge {rel_path}: {_reason} — "
                         f"kept {_n_ok}/{len(_raw_subs)} as separate sibling(s)")
        for _gen_attempt in range(2):
            if _did_split or block:
                break  # split-and-merge ran (or single-shot already has a block)
            if _gen_attempt == 0:
                _query = prompt
            else:
                # Medium retry — full prompt triggered an empty response (model silently
                # refuses on ~120K+ char inputs). Drop heavy context sections (siblings,
                # integration tests, strategy, history, old block) but keep source,
                # uncovered functions, symbols, and file constraints. Targets ~40-60K
                # chars — well below the silent-failure threshold.
                _medium_rules = (
                    _placement_rules
                    + _cluster_rules + "\n"
                    + _TEST_CONTRACT
                    + f"{_file_constraints_section}"
                )
                _query = (
                    f"You are writing Rust unit tests for `{rel_path}`.\n"
                    f"Target file: {rel_path}\n"
                    f"{_describe_test_environment()}"
                    f"{secs['cargo']}{secs['full_source']}{secs['unexecuted_fns']}{secs['fn_bodies']}{secs['behaviours']}{secs['symbols']}"
                    f"{_medium_rules}"
                    f"\nWrite a `tests/` integration-test file at `{_integ_rel}` covering the uncovered lines shown above{_calls_clause(_file_fn_targets)}.\n"
                    f"{output_format}\n"
                )
            # think=None: the transport's per-model default decides (on for
            # every profiled vLLM model; `adapters/llm_transports.resolve_thinking`).
            response = ask(_query, model=_TASK0C_MODEL, think=None)
            _pass_had_work = True  # LLM ran; empty extract is not "all files filtered"

            # Brace-counter extraction — no regex, handles any attribute/whitespace variation.
            block = extract_test_mod(response)
            if block:
                block = _strip_trailing_file_tag(block)
            if block and _block_is_prose(block):
                block = ""
            # Reject skeleton-only: model echoed the scaffold template unchanged —
            # all #[test] functions have empty or comment-only bodies.
            if block and not _has_nonempty_test_body(block):
                if _flags_unreachable((block or "") + (response or "")):
                    file_giveup_counts[rel_path] = file_giveup_counts.get(rel_path, 0) + 1
                    _log(f"  [{module_name}] LLM flagged {rel_path} as unreachable — discarding (no EXCL, stall counter applies; give-up {file_giveup_counts[rel_path]}).")
                    _save_stream_log(_gen_attempt, _query, response or "", "llm_giveup")
                else:
                    _log(f"  [{module_name}] Skeleton-only response for {rel_path} — discarding.")
                    _save_stream_log(_gen_attempt, _query, response or "", "skeleton_only")
                block = ""
            if block:
                break
            _raw_snippet = response[:300].replace('\n', ' ') if response else "(no output)"
            if _gen_attempt == 0:
                _log(f"  [{module_name}] Empty generation for {rel_path} — retrying. Raw: {_raw_snippet}")
                _save_stream_log(_gen_attempt, _query, response or "", "empty_generation")
            else:
                _log(f"  [{module_name}] Empty generation for {rel_path} (retry also failed). Raw: {_raw_snippet}")
                _save_stream_log(_gen_attempt, _query, response or "", "empty_generation_retry")
        if block:
            # Single-shot: finalize + commit the one generated block.
            _aimed_lines |= uncov_lines
            _status, block = _finalize_and_commit(block, _pass_n, _raw_text)
            if _status == "no_tests":
                _fn_empty = aimed_fn_outcomes(
                    _file_fn_targets, _aimed_lines, _ran_lines, "no_tests")
                _fn_outcomes.update(_fn_empty)
                _fn_attempted.update(_fn_empty)
                continue
            _block_committed = (_status == "committed")
            if _block_committed:
                _sibling_path = project_dir / gen_tests_integration_rel(abs_path.stem, _pass_n)
                _ran_lines |= uncov_lines
        elif not _did_split:
            _log_task0c_attempt(project_dir, rel_path, attempt, "", "", "no_block")
            _log(f"  [{module_name}] No test block extracted for {rel_path}.")

        # Charged before the quick-cov gate below may revert the block: a block
        # that compiled, ran and added no line is exactly an attempt that did
        # not reach its function.
        _fn_pass = aimed_fn_outcomes(_file_fn_targets, _aimed_lines, _ran_lines,
                                     "compile_failed")
        _fn_outcomes.update(_fn_pass)
        _fn_attempted.update(_fn_pass)

        # Quick per-file coverage snapshot after a successful append.
        _qcov_after: int | None = None
        if _block_committed:
            _qcov_result = _quick_file_coverage(project_dir, module_name, rel_path)
            if _qcov_result is not None:
                _qcov, _qcov_able = _qcov_result
                _qcov_after = _qcov
                _qpct = (_qcov / _qcov_able * 100.0) if _qcov_able else 0.0
                _log(f"  [{module_name}] {rel_path} quick-cov: {_qcov}/{_qcov_able} ({_qpct:.1f}%)")
                _emit("task0c.quick_cov", file=rel_path,
                      data={"covered": _qcov, "coverable": _qcov_able, "pct": _qpct})
                # Track whether THIS block actually moved the metric. If the
                # previous quick-cov for this file was already known and the
                # new value didn't increase, this block is concrete evidence
                # that the LLM's current strategy isn't working — feed it
                # to the deep retrospective at stall_n == RETRO_DEEP_AT_STALL,
                # and immediately REVERT the block so the source file isn't
                # cluttered with dead gen_tests siblings that never produced
                # coverage. _pass_n is recomputed from disk on the next
                # generation attempt, so the slot is naturally reused.
                _prev_qcov = file_last_qcov.get(rel_path)
                file_last_qcov[rel_path] = _qcov
                _pinned_now = _file_pinned_now.get(rel_path, frozenset())
                if (_prev_qcov is not None and _qcov <= _prev_qcov
                        and _behaviour_target > 0 and _pinned_now):
                    # No new line, but a harvested observable the oracle did
                    # not carry before — that is the block's whole value.
                    _log(f"  [{module_name}] {rel_path} — block added no line coverage "
                         f"(was {_prev_qcov}, now {_qcov}) but pins {len(_pinned_now)} "
                         f"behaviour(s) — kept.")
                elif _prev_qcov is not None and _qcov <= _prev_qcov:
                    file_failed_blocks.setdefault(rel_path, []).append(block)
                    _log(f"  [{module_name}] {rel_path} — block produced no coverage "
                         f"increase (was {_prev_qcov}, now {_qcov}); reverting.")
                    _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                    _block_committed = False
                    for _site, _why in _fn_pass.items():
                        if _why == "ran":
                            _fn_outcomes[_site] = "reverted"
                    file_last_qcov[rel_path] = _prev_qcov  # restore tracker too

        # Inner quick-retry: if quick-cov didn't reach 100% on a stalled or
        # few-lines-remaining file, immediately try a second focused generation
        # targeting only the still-uncovered lines with explicit branch hints.
        # Cheaper than an outer retry (no full grcov run, smaller prompt).
        _INNER_RETRY_LIMIT = 2
        if (
            _block_committed
            and _qcov_after is not None
            and _qcov_able > 0
            and _qcov_after < _qcov_able
            and file_stall_counts.get(rel_path, 0) > 0
        ):
            _src_lines_for_guard = existing_text.splitlines()
            _guard_hints = []
            for _lno in sorted(uncov_lines):
                _g = _extract_branch_guard(_src_lines_for_guard, _lno)
                if _g:
                    _guard_hints.append(f"  line {_lno}: `{_g}`")
            _guard_block = (
                "Branch conditions to exercise:\n" + "\n".join(_guard_hints) + "\n"
            ) if _guard_hints else ""

            for _ir in range(_INNER_RETRY_LIMIT):
                _emit("task0c.quick_retry", file=rel_path,
                      data={"retry": _ir + 1, "max": _INNER_RETRY_LIMIT})
                _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1}/{_INNER_RETRY_LIMIT} "
                     f"— {len(uncov_lines)} line(s) still uncovered.")
                _retry_pass_n = _next_gen_tests_n(abs_path)
                _retry_rel = gen_tests_integration_rel(abs_path.stem, _retry_pass_n)
                _retry_prompt = (
                    f"The test block you just wrote for `{rel_path}` compiled and ran "
                    f"but did NOT cover these specific lines: {sorted(uncov_lines)}\n\n"
                    f"{_guard_block}"
                    f"The function ran but took the OTHER branch. "
                    f"Write a NEW `tests/` integration-test file at `{_retry_rel}` containing "
                    f"ONLY tests that exercise the specific branch conditions above. "
                    f"Do not repeat tests from the existing block. Use `use {module_name}::*;`, "
                    f"not `use super::*;`.\n\n"
                    f"Source (uncovered lines marked):\n```rust\n"
                    f"{_annotate_uncovered(existing_text, uncov_lines)[:_SRC_SNIP_CAP]}\n```\n\n"
                    f"Existing test block (already on disk — do NOT duplicate):\n```rust\n"
                    f"{_old_block[:_SRC_SNIP_CAP] if _old_block else '(none)'}\n```\n\n"
                    f"Output ONLY the new file wrapped in <FILE path=\"{_retry_rel}\">...</FILE>."
                )
                _retry_resp = ask(_retry_prompt, model=_TASK0C_MODEL,
                                  think=None)
                _retry_block = extract_test_mod(_retry_resp)
                if not _retry_block:
                    _log(f"  [{module_name}] Quick-retry {_ir + 1}: no block extracted.")
                    break
                _retry_block = _strip_trailing_file_tag(_retry_block)
                _retry_block = _inject_allow_before_any_mod(_retry_block, _allow_line)
                _current_text = abs_path.read_text(encoding="utf-8")
                _retry_sibling = project_dir / _retry_rel
                _retry_sibling.parent.mkdir(parents=True, exist_ok=True)
                _retry_gate = _world_gate(_current_cluster) if _current_cluster else ""
                _retry_body = as_integration_test(
                    _retry_block, crate=module_name, allow_line=_allow_line)
                if _retry_gate:
                    _retry_body = f"#![cfg({_retry_gate})]\n" + _retry_body
                _retry_sibling.write_text(_retry_body, encoding="utf-8")
                _retry_check = subprocess.run(
                    ["cargo", "test", "--no-run", "-p", module_name],
                    cwd=project_dir, capture_output=True, text=True,
                    timeout=BUILD_TIMEOUT, env=_cargo_env(),
                )
                if _retry_check.returncode != 0:
                    _log(f"  [{module_name}] Quick-retry {_ir + 1}: compile failed — reverting.")
                    _revert_gen_tests_pass(abs_path, _current_text, _retry_sibling)
                    break
                # Compiling is not passing: the settle runs the block in the
                # world that generated it before any other, and a block that
                # fails there never enters the suite.
                if _settle_test_gate(project_dir, module_name, _retry_sibling,
                                     _retry_block, _current_cluster) is None:
                    _log(f"  [{module_name}] Quick-retry {_ir + 1}: compiled but its own "
                         "tests do not pass — reverting.")
                    _revert_gen_tests_pass(abs_path, _current_text, _retry_sibling)
                    break
                _log(f"  [{module_name}] Quick-retry {_ir + 1}: compiled and passes — "
                     "checking coverage.")
                _qr_result = _quick_file_coverage(project_dir, module_name, rel_path)
                if _qr_result is not None:
                    _qr_cov, _qr_able = _qr_result
                    _qr_pct = (_qr_cov / _qr_able * 100.0) if _qr_able else 0.0
                    _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1} cov: "
                         f"{_qr_cov}/{_qr_able} ({_qr_pct:.1f}%)")
                    if _qr_cov > _qcov_after:
                        _log(f"  [{module_name}] Quick-retry {_ir + 1}: improvement "
                             f"+{_qr_cov - _qcov_after} lines — stopping inner retries.")
                        _qcov_after = _qr_cov
                        _pass_had_work = True
                        break
                else:
                    break
            _emit("task0c.quick_retry_done", file=rel_path)  # clear the retry counter

        # Per-file stall detection: track whether uncovered line count improved.
        prev_best = file_best_uncov.get(rel_path, len(uncov_lines) + 1)
        if len(uncov_lines) < prev_best:
            newly_covered = file_prev_uncov_lines.get(rel_path, set()) - uncov_lines
            file_best_uncov[rel_path] = len(uncov_lines)
            file_stall_counts[rel_path] = 0
            file_giveup_counts[rel_path] = 0   # progress resets the give-up streak
            file_loops.setdefault(rel_path, LoopState()).record(
                Attempt(metric=float(-len(uncov_lines)), improved=True))
            # Capture what worked so next attempt can reuse the same patterns.
            if _old_block:
                _condensed_working = _condense_rust_to_signatures(_old_block, "gen_tests.rs")
                _covered_sample = sorted(newly_covered)[:8]
                _covered_note = (f" Lines newly covered: {_covered_sample}." if _covered_sample else "")
                file_what_worked[rel_path] = (
                    f"Previous attempt covered {prev_best - len(uncov_lines)} more line(s).{_covered_note} "
                    f"Reuse these construction patterns:\n{_condensed_working[:_WORKING_CAP]}"
                )
            file_retrospectives.pop(rel_path, None)  # stale retrospective no longer valid
        else:
            file_stall_counts[rel_path] = file_stall_counts.get(rel_path, 0) + 1
            stall_n = file_stall_counts[rel_path]
            _note = ("model judged these lines unreachable"
                     if file_giveup_counts.get(rel_path, 0) > _giveup_before
                     else "compiled but added no coverage")
            file_loops.setdefault(rel_path, LoopState()).record(
                Attempt(metric=float(-len(uncov_lines)), improved=False, note=_note))
            # Retrospective is fired twice:
            #   stall_n == 1                — initial classification from source alone
            #   stall_n == RETRO_DEEP_AT_STALL — re-classification with concrete
            #     evidence of failed attempts. Closes the feedback loop: when the
            #     first call said TESTABLE and the LLM has since committed several
            #     blocks that didn't move the metric, the classifier should revise
            #     that call to UNREACHABLE / NEEDS_INTERNALS instead.
            # Regenerate the evidence-rich retrospective on EVERY stall (not
            # once) so each of the FILE_STALL_LIMIT attempts is informed by
            # the latest accumulated failed-attempt evidence.
            _fire_deep = (
                stall_n >= RETRO_DEEP_AT_STALL
                and bool(file_failed_blocks.get(rel_path))
            )
            if (stall_n >= 1) and (fn_bodies.strip() or _file_fn_targets) and not ctx.behaviour_pass:
                if _fire_deep:
                    _log(f"  [{module_name}] {rel_path} stalled at {stall_n} with "
                         f"{len(file_failed_blocks[rel_path])} failed attempt(s) — "
                         f"deep retrospective with concrete evidence.")
                else:
                    _log(f"  [{module_name}] No improvement for {rel_path} — running retrospective.")
                # Pass ALL uncovered functions to the retrospective (no max_fns cap)
                # so the LLM classifies every function in one shot, not one per attempt.
                _retro_fn_bodies = _extract_uncovered_functions(existing_text, uncov_lines, max_fns=0)
                retro = _task0c_retrospective(
                    rel_path, existing_text, _retro_fn_bodies, _old_block, module_name,
                    attempt_history=attempt_history,
                    uncov_lines=uncov_lines,
                    unproductive_blocks=file_failed_blocks.get(rel_path, []) if _fire_deep else None,
                    consecutive_giveups=file_giveup_counts.get(rel_path, 0) if _STALL_CONTEXT else 0,
                )
                if retro:
                    file_retrospectives[rel_path] = retro
                    _log(f"  [{module_name}] Retrospective: {retro[:300]}")
                    # Immediately pragma UNREACHABLE/CFG_GATED functions — no point retrying
                    # test generation for code that is intrinsically unmeasurable.
                    _retro_cats = _parse_retrospective_categories(retro)
                    _fn_verdicts.update(unambiguous_verdicts(_file_fn_targets, _retro_cats))
                    _pragma_fns, _pragma_withheld = withhold_pending(
                        _retro_cats["UNREACHABLE"] + _retro_cats["CFG_GATED"],
                        _file_fn_targets)
                    if _pragma_withheld:
                        _log(f"  [{module_name}] {rel_path} — not pragma'd, still inside the "
                             f"function attempt budget: {_pragma_withheld} (the classification "
                             "is a hypothesis until the budget is spent).")
                    if _pragma_fns:
                        _log(f"  [{module_name}] Targeted pragma for: {_pragma_fns}")
                        _pragma_ok = _task0c_targeted_pragma(
                            project_dir, module_name, rel_path,
                            uncov_lines, _pragma_fns, _TASK0C_MODEL,
                        )
                        if _pragma_ok:
                            # Run quick-cov immediately with the new pragmas on disk.
                            # If the file is now fully covered/excluded, skip it for
                            # all future generation passes — no point burning LLM calls.
                            _post_pragma = _quick_file_coverage(project_dir, module_name, rel_path)
                            if _post_pragma is not None:
                                _pp_cov, _pp_able = _post_pragma
                                _pp_pct = (_pp_cov / _pp_able * 100.0) if _pp_able else 100.0
                                _log(f"  [{module_name}] {rel_path} post-pragma quick-cov: "
                                     f"{_pp_cov}/{_pp_able} ({_pp_pct:.1f}%)")
                                if _pp_cov >= _pp_able and not _file_fn_targets:
                                    _log(f"  [{module_name}] {rel_path} fully covered after pragma — "
                                         f"skipping further generation.")
                                    skipped_files.add(rel_path)
                                elif _pp_cov >= _pp_able:
                                    # A pragma is a line classification; the
                                    # function criterion is measured on FN
                                    # records. Skipping here would strand the
                                    # file's unexecuted functions with no
                                    # attempt ever charged, holding the stop
                                    # open for nothing.
                                    _log(f"  [{module_name}] {rel_path} fully covered after pragma, "
                                         f"but {len(_file_fn_targets)} function(s) no test executes "
                                         "remain targeted — not skipping.")
                            # Reset stall so the file gets another generation pass for
                            # any remaining TESTABLE/NEEDS_INTERNALS lines.
                            file_stall_counts[rel_path] = 0
            if stall_n >= FILE_STALL_LIMIT:
                _log(f"  [{module_name}] {rel_path} stalled for {FILE_STALL_LIMIT} consecutive "
                     f"attempts with no improvement — skipping this file.")
                skipped_files.add(rel_path)

        # Update snapshot for next attempt's line-coverage delta.
        file_prev_uncov_lines[rel_path] = set(uncov_lines)

    # A ceiling is a MEASURED claim — every uncovered line that survives is inside
    # an excluded cfg block or is an LLVM artifact, so no test could reach it — and
    # the pass has just counted them. It used to be inferred from "no file set
    # `_pass_had_work`", which was set only on a committed block, and the log line
    # asserted the cfg claim without ever checking it. On 2026-08-27 run
    # `2881435-34981ab0` logged 63 `llm.request` events and extracted nothing (fenced replies),
    # so both passes over anyhow/stable reported a ceiling while their own
    # post-filter counts listed 119 actionable uncovered lines across 8 files —
    # 4 in backtrace.rs, 3 ensure.rs, 4 lib.rs, 9 ptr.rs, 8 fmt.rs, 14 wrapper.rs,
    # 23 context.rs, 54 error.rs. Two such passes are CEILING_CONFIRM, so the
    # cluster was accepted at 83.9% against its own ~98.1% ceiling estimate.
    _pass_outcome = coverage_pass_outcome(
        actionable_lines=_pass_actionable, had_model_call=_pass_had_work)
    if _pass_outcome == "ceiling":
        _log(f"  [{module_name}] All files filtered (cfg/artifact/proved unreachable/budget spent) "
             "— no actionable uncovered lines. Stopping.")
        return _PhaseCOutcome("break", at_ceiling=True, blocks_admitted=_blocks_admitted,
                              fn_attempted=frozenset(_fn_attempted),
                              fn_outcomes=tuple(_fn_outcomes.items()),
                              fn_verdicts=tuple(_fn_verdicts.items()))
    # If every remaining uncovered file is now skipped, die with a report. The
    # function criterion holds the stop open above the line target, so a pass
    # whose every file carrying an unexecuted function is skipped cannot make
    # progress either — without this it would re-measure coverage every
    # attempt to the hard cap with nothing left to generate for.
    remaining = set(uncovered_by_file.keys()) - skipped_files
    if not remaining and skipped_files and (coverage < _COVERAGE_TARGET or fn_targets_by_file):
        _log(f"\n  [{module_name}] HARD BLOCK: all uncovered files are stalled. "
             f"Coverage: {coverage:.1f}% (target {_COVERAGE_TARGET:.0f}%); "
             f"{sum(len(v) for v in fn_targets_by_file.values())} unexecuted function(s) targeted.")
        _log("  Stalled files and their uncovered line counts:")
        for sf in sorted(skipped_files):
            n_uncov = len(uncovered_by_file.get(sf, (None, set()))[1])
            history = _get_task0c_history(project_dir, sf)
            retro = file_retrospectives.get(sf, "")
            retro_note = f"\n      retrospective: {retro[:300]}" if retro else ""
            _log(f"    {sf}: {n_uncov} uncovered lines. Last attempts:\n{history}{retro_note}")
        _log("  Action required: improve the generation prompt or add manual tests for these files.")
        return _PhaseCOutcome("return", result=(coverage, covered_lines, False, ""),
                              blocks_admitted=_blocks_admitted,
                              fn_attempted=frozenset(_fn_attempted),
                              fn_outcomes=tuple(_fn_outcomes.items()),
                              fn_verdicts=tuple(_fn_verdicts.items()))

    # Reachable work and not one model call, with files still dispatchable: a
    # generation or routing defect, never a ceiling. This sits BELOW the
    # stalled-files report on purpose — "every file is stalled" is the specific,
    # already-diagnosed shape of the same state and owns its own outcome, and
    # the false ceiling used to jump over it. Breaking without `at_ceiling`
    # leaves the module on the stall clock, so a transient (a dead endpoint)
    # recovers on the next pass and a permanent one hard-blocks with that
    # report — neither marks the module augmented at a number nothing measured.
    if _pass_outcome == "no_model_call":
        _log(f"  [{module_name}] PASS MADE NO MODEL CALL against {_pass_actionable} "
             f"actionable uncovered line(s) in {len(_active_files)} file(s) — a "
             f"generation/routing defect, not a coverage ceiling. Not accepting a ceiling.")
        _emit("task0c.pass_no_work", data={"actionable": _pass_actionable,
                                           "files": len(_active_files)})
        return _PhaseCOutcome("break", at_ceiling=False, blocks_admitted=_blocks_admitted,
                              fn_attempted=frozenset(_fn_attempted),
                              fn_outcomes=tuple(_fn_outcomes.items()),
                              fn_verdicts=tuple(_fn_verdicts.items()))

    return _PhaseCOutcome("continue", blocks_admitted=_blocks_admitted,
                          fn_attempted=frozenset(_fn_attempted),
                              fn_outcomes=tuple(_fn_outcomes.items()),
                              fn_verdicts=tuple(_fn_verdicts.items()))


def _diagnose_and_fix_regression(
    *,
    project_dir: Path,
    module_name: str,
    module_source_paths: list[Path],
    existing_test_files: set[str],
    delta_lines: int,
    cargo_env: Callable[[], dict[str, str]],
    subprocess: Any,
    build_timeout: int,
    generate_fix_loop: Callable[..., tuple[bool, str]],
    model: str,
    log: Callable[[str], None],
) -> None:
    """A large coverage drop usually means a generated test now fails at runtime
    and corrupts grcov profraw data. Run the tests to confirm, and if they fail,
    repair them before generating more (which would only pile failures on top).
    Extracted from the augmentation loop."""
    log(f"  [{module_name}] Coverage regression {delta_lines:+d} lines — running tests to detect runtime failures.")
    reg_result = subprocess.run(
        ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1", "--no-capture"],
        cwd=project_dir, capture_output=True, text=True,
        timeout=build_timeout, env=cargo_env(),
    )
    if reg_result.returncode != 0:
        log(f"  [{module_name}] Test failures confirmed — running runtime fix loop.")
        generate_fix_loop(
            project_dir=project_dir,
            check_cmd=f"cargo test -p {module_name} -- --test-threads=1",
            source_paths=module_source_paths,
            existing_test_files=existing_test_files,
            model=model,
            max_fix_attempts=3,
            timeout=build_timeout,
        )
    else:
        log(f"  [{module_name}] Tests pass but coverage regressed — likely stale profraw; retrying.")


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    deps: Task0cDeps,
    crate_type: str = "",
    existing_test_files: set[str] | None = None,
    project_context: str = "",
    coverage_strategy: str = "",
) -> tuple[float, int, bool, str]:
    """
    Generate integration tests (`tests/{stem}_gen_{N}.rs`) for a module
    until tarpaulin reports 100% coverage or MAX_RETRIES is exhausted.
    Returns final coverage percentage. New writes go under `tests/`; leftover
    `*.gen_tests_*.rs` siblings from earlier runs stay readable.
    """
    # Rebind injected collaborators as locals so the body below is verbatim.
    BUILD_TIMEOUT = deps.BUILD_TIMEOUT
    _COVERAGE_TARGET = deps.COVERAGE_TARGET
    _FIX_ERR_CAP = deps.FIX_ERR_CAP
    _SIBLING_CAP = deps.SIBLING_CAP
    _SRC_SNIP_CAP = deps.SRC_SNIP_CAP
    _TASK0C_FIX_THINKING_DIRECTIVE = deps.TASK0C_FIX_THINKING_DIRECTIVE
    _TASK0C_MODEL = deps.TASK0C_MODEL
    _TASK0C_THINKING_DIRECTIVE = deps.TASK0C_THINKING_DIRECTIVE
    _WORKING_CAP = deps.WORKING_CAP
    _annotate_uncovered = deps.annotate_uncovered
    _block_is_prose = deps.block_is_prose
    _build_gen_tests_decl = deps.build_gen_tests_decl
    _build_task0c_prompt_sections = functools.partial(_engine_build_prompt_sections, deps)
    _cargo_env = deps.cargo_env
    _condense_rust_to_signatures = deps.condense_rust_to_signatures
    _count_test_attrs = deps.count_test_attrs
    _derive_gentest_prelude = deps.derive_gentest_prelude
    _describe_test_environment = deps.describe_test_environment
    _error_signatures_from_output = deps.error_signatures_from_output
    _extract_branch_guard = deps.extract_branch_guard
    _extract_gen_tests_body = deps.extract_gen_tests_body
    _extract_public_symbols = deps.extract_public_symbols
    _extract_uncovered_functions = deps.extract_uncovered_functions
    _filter_cfg_excluded_lines = deps.filter_cfg_excluded_lines
    _filter_cfg_incompatible_lines = deps.filter_cfg_incompatible_lines
    _flags_unreachable = deps.flags_unreachable
    _gen_tests_path = deps.gen_tests_path
    _get_task0c_history = functools.partial(_engine_get_history, deps)
    _has_nonempty_test_body = deps.has_nonempty_test_body
    _inject_allow_before_any_mod = deps.inject_allow_before_any_mod
    _inject_allow_before_gen_mod = deps.inject_allow_before_gen_mod
    _inject_gen_prelude_imports = deps.inject_gen_prelude_imports
    _is_gen_tests_file = deps.is_gen_tests_file
    _is_structural_line = deps.is_structural_line
    _load_file_constraints = deps.load_file_constraints
    _log_task0c_attempt = functools.partial(_engine_log_attempt, deps)
    _mark_phase = deps.mark_phase
    _match_gen_tests_sibling = deps.match_gen_tests_sibling
    _next_gen_tests_n = deps.next_gen_tests_n
    _nightly_rules_for_cluster = deps.nightly_rules_for_cluster
    _parse_attempt_number = deps.parse_attempt_number
    _parse_retrospective_categories = deps.parse_retrospective_categories
    _partition_uncovered_lines = deps.partition_uncovered_lines
    _prelude_import_names = deps.prelude_import_names
    _quick_file_coverage = deps.quick_file_coverage
    _read_all_gen_tests = deps.read_all_gen_tests
    _read_state = deps.read_state
    _revert_gen_tests_pass = deps.revert_gen_tests_pass
    _select_relevant_siblings = deps.select_relevant_siblings
    _set_current = deps.set_current
    _source_hash = deps.source_hash
    _strip_all_gen_tests_artifacts = deps.strip_all_gen_tests_artifacts
    _strip_gen_tests_decl_n = deps.strip_gen_tests_decl_n
    _strip_gen_tests_decls = deps.strip_gen_tests_decls
    _strip_trailing_file_tag = deps.strip_trailing_file_tag
    _check_stop = deps.check_stop
    _task0c_mutation_pass = functools.partial(_engine_mutation_pass, deps)
    _task0c_pragma_pass = functools.partial(_engine_pragma_pass, deps)
    _task0c_retrospective = functools.partial(_engine_retrospective, deps)
    _task0c_targeted_pragma = functools.partial(_engine_targeted_pragma, deps)
    _test_augmentor_factory = deps.test_augmentor_factory
    ask = deps.ask
    extract_files = deps.extract_files
    generate_fix_loop = deps.generate_fix_loop
    is_test_file = deps.is_test_file
    write = deps.write
    subprocess = deps.subprocess
    _current_cluster = deps.get_current_cluster()
    _coverage_provider = deps.coverage_provider
    _log = log
    _emit = emit
    if existing_test_files is None:
        existing_test_files = set()

    # Cluster label for log messages — shows which compiler configuration is active.
    _cluster_label = _current_cluster.name if _current_cluster else "stable"

    # Split-and-merge degree for per-file generation, from the single global
    # PIPELINE_PARALLELISM knob (legacy TASK0C_SPLIT_N is dead). 1 = legacy
    # single-shot. >1 = partition the file's uncovered functions into N disjoint
    # subsets, generate a test block for each CONCURRENTLY (shared-prefix prompts
    # batch on the GPU), then commit each block through the normal compile-fix
    # loop so survivors accumulate (additive coverage). Generation is parallel;
    # every cargo/coverage call stays serial (no shared-target/ race). Coverage
    # (quick-cov) runs once on the merged result. See _partition_uncovered_lines.
    _split_n = max(1, int(os.getenv("PIPELINE_PARALLELISM", "1")))
    if _split_n > 1:
        _log(f"  [{module_name}/{_cluster_label}] split-and-merge generation enabled: "
             f"PIPELINE_PARALLELISM={_split_n} (subset blocks concurrent; compile+cov serial).")

    augmentor = _test_augmentor_factory()

    if not augmentor.is_applicable(crate_type):
        _log(f"  [{module_name}] crate type '{crate_type}' not applicable for augmentation — skipping.")
        return 100.0, 0, False, ""  # treat as done

    _preflight = _run_harness_preflight(
        project_dir=project_dir,
        module_name=module_name,
        module_dir=module_dir,
        augmentor=augmentor,
        ctx=_PreflightCtx(
            log=_log,
            emit=_emit,
            is_test_file=is_test_file,
            ask=ask,
            write=write,
            extract_files=extract_files,
            subprocess=subprocess,
            build_timeout=BUILD_TIMEOUT,
            fix_thinking_directive=_TASK0C_FIX_THINKING_DIRECTIVE,
            task0c_model=_TASK0C_MODEL,
            cargo_env=_cargo_env,
            match_gen_tests_sibling=_match_gen_tests_sibling,
            strip_gen_tests_decl_n=_strip_gen_tests_decl_n,
            strip_all_gen_tests_artifacts=_strip_all_gen_tests_artifacts,
            is_gen_tests_file=_is_gen_tests_file,
            error_signatures_from_output=_error_signatures_from_output,
            check_stop=_check_stop,
        ),
    )
    if _preflight is not None:
        return _preflight

    # Derive allow attributes and prelude imports from project source — no LLM.
    _allow_line, _prelude_block = _derive_gentest_prelude(project_dir)
    _prelude_names = _prelude_import_names(_prelude_block)

    # Pre-clean only if the harness is broken — if existing gen_tests compile,
    # keep them so accumulated coverage is never lost on re-entry.
    _precheck_ok, _ = augmentor.test_harness_compiles(project_dir, module_name)
    if not _precheck_ok:
        for src_file in _iter_module_rs(module_dir):
            if is_test_file(src_file, project_dir):
                continue
            # Don't recurse into siblings here — they're handled via their parent.
            if _is_gen_tests_file(src_file.name):
                continue
            try:
                n_removed = _strip_all_gen_tests_artifacts(src_file)
                if n_removed > 0:
                    _log(f"  [{module_name}] Pre-cleaned {n_removed} broken gen_tests "
                         f"sibling(s) for {src_file.relative_to(project_dir)}")
            except Exception:
                pass

    # Floor-stall semantics with the absolute cap raised so it's no
    # longer the binding constraint. `MAX_NO_IMPROVEMENT` below already
    # functions as the stall budget; we just surface both as env-configurable.
    max_attempts = int(os.getenv("TASK0C_HARD_CAP", "30"))
    attempt = 0
    # Resume attempt number if interrupted mid-run.
    _saved_phase = _read_state(project_dir)["phases"].get(f"task0c/{_cluster_label}/{module_name}", {})
    if _saved_phase.get("status") == "in_progress":
        _saved_attempt = _parse_attempt_number(_saved_phase.get("detail", ""))
        if _saved_attempt is not None:
            attempt = _saved_attempt - 1  # loop does attempt += 1 at top
            _log(f"  [{module_name}/{_cluster_label}] Resuming from attempt {_saved_attempt}/{max_attempts}")
    coverage = 0.0
    covered_lines = 0
    coverable_lines = 0
    files_data: list[PerFileCoverage] = []
    _functions: dict[tuple[str, int], int] | None = None
    _skip_first_measure = False
    # The function-execution criterion. Line coverage alone stopped this loop
    # at 95.2 % on anyhow's `nightly` cluster (run `330242-0be2c3e1`,
    # 2026-09-03) with functions no test ever entered, and those bodies then
    # stayed `unimplemented!()` through every fill gate to exit 1 at the end of
    # run `301146-2d8a5f06`. The line target now stops the loop only when every
    # function the cluster compiles is executed, or measured unreached after
    # `TASK0C_FN_ATTEMPTS` targeted attempts whose test blocks compiled and ran.
    # The record persists per cluster, so a resumed pass keeps the attempts
    # already spent.
    _fn_budget = max(1, int(os.getenv("TASK0C_FN_ATTEMPTS", "3")))
    _fn_inputs = FnCriterionInputs(enumerate_fns=deps.enumerate_fns,
                                   enumerate_mods=deps.enumerate_mods,
                                   measure_reachability=(
                                       deps.measure_reachability
                                       if os.getenv("TASK0C_FN_REACHABILITY", "1") != "0"
                                       else None))
    # Which functions no test can execute, proved from the compiler's MIR and
    # public index before the first targeted attempt. Taken once
    # per cluster entry: the tree's callers do not change while this runs.
    _fn_proofs, _fn_proof_reason = read_by_construction(
        project_dir, _current_cluster, _fn_inputs)
    _emit("task0c.fn_reachability", data={
        "cluster": _cluster_label, "module": module_name,
        "status": "ok" if not _fn_proof_reason else "unavailable",
        "reason": _fn_proof_reason,
        "proved": [{"site": site_label(k), "proof": v} for k, v in sorted(_fn_proofs.items())]})
    if _fn_proofs:
        _log(f"  [{module_name}/{_cluster_label}] {len(_fn_proofs)} function(s) no test can "
             "execute, proved from the compiler's MIR and public index: "
             + ", ".join(site_label(k) for k in sorted(_fn_proofs)[:12]))
    elif _fn_proof_reason:
        _log(f"  [{module_name}/{_cluster_label}] no reachability proof taken "
             f"({_fn_proof_reason}); the attempt budget decides alone.")
    # What the other clusters already spent on a body both compile. Read once
    # at entry: they are finished (or frozen) while this one runs.
    _fn_inherited: dict[Any, int] = {}
    if callable(deps.other_fn_records):
        _others = deps.other_fn_records(project_dir, _cluster_label, module_name)
        _fn_inherited = inherited_attempts(
            r for r in (_others if isinstance(_others, list) else [])
            if isinstance(r, FnCoverageRecord))
    _fn_record: FnCoverageRecord | None = None
    if callable(deps.load_fn_record):
        _loaded = deps.load_fn_record(project_dir, _cluster_label, module_name)
        _fn_record = _loaded if isinstance(_loaded, FnCoverageRecord) else None
    # The per-file line floor. A file the generation pass already gave up on
    # (its stall limit, in an earlier entry of this cluster) stays given up:
    # re-entering must not buy it a second budget.
    _file_floor_pct = _file_floor_of(deps)
    _file_floor: FileFloor | None = None
    _floor_stalled: frozenset[str] = frozenset()
    if _file_floor_pct > 0 and callable(deps.load_file_floor):
        _prior_floor = deps.load_file_floor(project_dir, _cluster_label, module_name)
        if isinstance(_prior_floor, FileFloor):
            _floor_stalled = _prior_floor.stalled
    # On resume, reload coverage from the previous run's grcov JSON + filecache
    # to avoid an expensive full re-run before the first gen attempt.
    if attempt > 0:
        _cached = _coverage_provider.load_cached(project_dir, module_name)
        if _cached is not None:
            coverage, files_data, covered_lines, coverable_lines = (
                _cached[0], _cached[1], _cached[2], _cached[3])
            _functions = getattr(_cached, "functions", None)
            _skip_first_measure = True
            _log(f"  [{module_name}/{_cluster_label}] Loaded coverage from previous run: "
                 f"{coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
    prev_covered = covered_lines if _skip_first_measure else -1
    no_improvement_count = 0
    MAX_NO_IMPROVEMENT = int(os.getenv(
        "TASK0C_MAX_STALLS", "5"
    ))  # Tightened — per-file retrospective fires first; this is the
    # global stall budget. Lower than the legacy 10 because the per-file
    # deep retrospective (RETRO_DEEP_AT_STALL=5) is the heavy lifter.
    # Per-file budget: up to 5 INFORMED attempts before skipping a file. Each
    # stalled attempt regenerates the deep retrospective with the latest
    # accumulated failed-attempt evidence (see below), so these are 5 evidence-
    # driven shots, not blind re-rolls.
    FILE_STALL_LIMIT = int(os.getenv("TASK0C_FILE_STALL_LIMIT", "5"))
    # Regenerate the evidence-rich retrospective on EVERY stall from this count
    # on (default 1 = from the first 0-coverage attempt). Plain re-rolls are
    # coverage-BLIND — the model gets compile/test errors (Phase A fix loop) but
    # never the "compiled yet covered nothing" signal, which only the
    # retrospective injects. Firing it each iteration (with fresh evidence)
    # gives the model informed, varying shots instead of one stale retro reused.
    RETRO_DEEP_AT_STALL = int(os.getenv("TASK0C_RETRO_DEEP_AT_STALL", "1"))
    _hist = Task0cFileHistory()
    # Track concrete attempts that compiled but added zero coverage to the
    # targeted lines. Fed to the deep retrospective so the classifier can see
    # which strategies the LLM has already tried — and revise its TESTABLE
    # call to UNREACHABLE when evidence demands it.
    # Give the generation model META-AWARENESS that it is stalling (today it gets
    # none — it re-tries blind), and FEED the consecutive give-up signal to the retrospective so
    # genuinely-unreachable lines are classified (and pragma'd) faster instead of burning the full
    # stall budget. Env-gated so a fresh run can be A/B'd against the current (pre-fix) run.
    _STALL_CONTEXT = os.getenv("TASK0C_STALL_CONTEXT", "1") == "1"
    _at_ceiling = False                      # True when a full pass produced no actionable lines
    while attempt < max_attempts:
        attempt += 1
        _log(f"  [{module_name}/{_cluster_label}] Attempt {attempt}/{max_attempts}")
        _emit("task0c.attempt", data={"attempt": attempt, "max_attempt": max_attempts})

        # --- Phase A: compile check + targeted fix ---
        # Use generate_fix_loop: run compile check, if it fails ask LLM to fix
        # specific files rather than regenerating from scratch.
        # Only source files with #[cfg(test)] blocks are candidates — these hold
        # the generated tests that may have syntax errors.
        module_source_paths = [
            f for f in _iter_module_rs(module_dir)
            if not is_test_file(f, project_dir)
        ]
        compile_ok, _ = generate_fix_loop(
            project_dir=project_dir,
            check_cmd=f"cargo test -p {module_name} --no-run",
            source_paths=module_source_paths,
            existing_test_files=existing_test_files,
            model=_TASK0C_MODEL,
            max_fix_attempts=1,  # one fix attempt per outer attempt; outer loop retries
            timeout=BUILD_TIMEOUT,
        )
        if not compile_ok:
            _log(f"  [{module_name}] Still does not compile after fix — retrying outer loop.")
            continue

        # --- Phase B: measure coverage ---
        if _skip_first_measure:
            _skip_first_measure = False  # only skip once; always re-measure after first gen
        else:
            _measured = augmentor.measure_coverage(project_dir, module_name, module_dir)
            coverage, files_data, covered_lines, coverable_lines = (
                _measured[0], _measured[1], _measured[2], _measured[3])
            _functions = getattr(_measured, "functions", None)
            _log(f"  [{module_name}/{_cluster_label}] Coverage: {coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
        _fn_reading = read_function_execution(project_dir, _current_cluster, _functions,
                                              _fn_inputs, _fn_proofs)
        _fn_record = fold_reading(_fn_record, _fn_reading,
                                  attempt_budget=_fn_budget, inherited=_fn_inherited)
        _proved_targets = (_fn_reading.proved_targets
                           if isinstance(_fn_reading, FunctionExecution) else ())
        _contradicted = [site_label(k) for k in _fn_proofs
                         if k in set(getattr(_fn_record, "executed", ()))]
        if _contradicted:
            # A proved-unreachable function that a test executed. The proof
            # speaks for `tests/`; an in-crate unit test can run a private fn,
            # which is benign, and anything else is a defect in the proof.
            # Either way the measurement wins (the record already holds the
            # function as executed).
            _log(f"  [{module_name}/{_cluster_label}] COUNTEREXAMPLE to the reachability proof: "
                 + ", ".join(_contradicted)
                 + " (executed although proved unreachable from tests/)")
            _emit("task0c.fn_reachability_contradicted", data={
                "cluster": _cluster_label, "module": module_name, "sites": _contradicted})
        _record_fn_reading(deps, project_dir, module_name, _cluster_label, attempt, _fn_record)
        if _file_floor_pct > 0:
            _file_floor = _read_file_floor(
                deps, project_dir, module_dir, _current_cluster, files_data, _fn_reading,
                _fn_record, stalled=_floor_stalled | _hist.skipped_files, floor=_file_floor_pct)
            _log(f"  [{module_name}/{_cluster_label}] file floor: {_file_floor.summary()}")
            _emit("task0c.file_floor", data={"cluster": _cluster_label, "module": module_name,
                                             "attempt": attempt, **_file_floor.event_data()})
            if callable(deps.save_file_floor):
                deps.save_file_floor(project_dir, _cluster_label, module_name, _file_floor)
        _mark_phase(project_dir, f"task0c/{_cluster_label}/{module_name}", "in_progress",
                    detail=f"{coverage:.1f}% attempt {attempt}", source_hash=_source_hash(project_dir))
        _set_current(project_dir, f"task0c/{_cluster_label}/{module_name} / {coverage:.1f}% attempt {attempt}")

        if coverage >= _COVERAGE_TARGET:
            if _fn_record.measured and not _fn_record.accounted:
                _held = [p.target.label for p in _fn_record.pending]
                _log(f"  [{module_name}/{_cluster_label}] {coverage:.1f}% line coverage meets the "
                     f"target, but {len(_held)} function(s) the cluster compiles are executed by "
                     f"no test and still inside their {_fn_budget}-attempt budget — NOT stopping: "
                     + ", ".join(_held[:12]) + (f", … (+{len(_held) - 12})" if len(_held) > 12 else ""))
                _emit("task0c.fn_stop_held", data={
                    "cluster": _cluster_label, "module": module_name, "attempt": attempt,
                    "coverage_pct": coverage, "pending": _held})
            elif _file_floor is not None and _file_floor.held:
                _held_files = _file_floor.held
                _log(f"  [{module_name}/{_cluster_label}] {coverage:.1f}% line coverage meets the "
                     f"target, but {len(_held_files)} file(s) are below the "
                     f"{_file_floor_pct:.0f}% floor on the lines a test can reach — NOT stopping: "
                     + "; ".join(f.describe() for f in _held_files))
                _emit("task0c.file_floor_held", data={
                    "cluster": _cluster_label, "module": module_name, "attempt": attempt,
                    "coverage_pct": coverage, "held": [f.rel for f in _held_files]})
            else:
                if not _fn_record.measured:
                    _log(f"  [{module_name}/{_cluster_label}] function execution NOT measured "
                         f"({_fn_record.reason}) — this stop reads line coverage alone.")
                _log(f"  [{module_name}] {coverage:.1f}% coverage reached (target {_COVERAGE_TARGET:.0f}%).")
                break

        # Stall detection: compare integer covered-line counts, not float percentages.
        # Float coverage can oscillate by ±0.01% between runs (denominator shifts when
        # generated test blocks change which lines tarpaulin counts). Integers are stable.
        delta_lines = covered_lines - prev_covered

        # --- Regression guard: if coverage dropped significantly, gen_tests likely have
        # runtime failures that corrupt grcov profraw data.  Diagnose and fix before
        # generating more tests (which would only pile more failing tests on top). ---
        if prev_covered > 0 and delta_lines < -50:
            _diagnose_and_fix_regression(
                project_dir=project_dir,
                module_name=module_name,
                module_source_paths=module_source_paths,
                existing_test_files=existing_test_files,
                delta_lines=delta_lines,
                cargo_env=_cargo_env,
                subprocess=subprocess,
                build_timeout=BUILD_TIMEOUT,
                generate_fix_loop=generate_fix_loop,
                model=_TASK0C_MODEL,
                log=_log,
            )
            prev_covered = covered_lines
            continue  # skip Phase C (no new tests until runtime failures are resolved)

        # Uniform loop.iter event on every attempt for monitor.py.
        _emit("loop.iter", data={
            "name": "task0c.augment",
            "module": module_name,
            "cluster": _cluster_label,
            "iter": attempt,
            "metric_new": coverable_lines - covered_lines,  # uncov lines
            "metric_best": coverable_lines - max(covered_lines, prev_covered),
            "improved": delta_lines > 0,
            "stalled": no_improvement_count,
        })
        if delta_lines <= 0:
            no_improvement_count += 1
            _log(f"  [{module_name}] No improvement ({covered_lines}/{coverable_lines} lines, delta {delta_lines:+d}) — stall count {no_improvement_count}/{MAX_NO_IMPROVEMENT}")
            if no_improvement_count >= MAX_NO_IMPROVEMENT:
                _log(f"  [{module_name}] Coverage stalled at {coverage:.1f}% — switching to pragma pass.")
                _task0c_pragma_pass(
                    project_dir, module_name, module_dir, files_data, _TASK0C_MODEL
                )
                # Re-measure after pragma pass — if 100%, we're done; if not, outer
                # loop will keep trying (or the outer no-improvement guard fires).
                break
        else:
            no_improvement_count = 0
        prev_covered = covered_lines


        _pc = _run_phase_c(
            project_dir=project_dir,
            module_name=module_name,
            module_dir=module_dir,
            project_context=project_context,
            coverage_strategy=coverage_strategy,
            attempt=attempt,
            coverage=coverage,
            covered_lines=covered_lines,
            files_data=files_data,
            hist=_hist,
            fn_targets=_fn_record.targets() if _fn_record.measured else (),
            proved=_proved_targets,
            spent=_spent_targets(_fn_reading, _fn_record),
            ctx=_PhaseCCtx(
                deps=deps,
                current_cluster=_current_cluster,
                cluster_label=_cluster_label,
                split_n=_split_n,
                allow_line=_allow_line,
                prelude_block=_prelude_block,
                prelude_names=_prelude_names,
                file_stall_limit=FILE_STALL_LIMIT,
                retro_deep_at_stall=RETRO_DEEP_AT_STALL,
                stall_context=_STALL_CONTEXT,
            ),
        )
        if _pc.fn_verdicts:
            _fn_record = note_verdicts(_fn_record, dict(_pc.fn_verdicts))
        if _pc.fn_attempted:
            # Charged now, judged at the next measurement: the attempt counts
            # toward `unreached` only if the reading after it still reads 0.
            _fn_record = note_attempt(_fn_record, _pc.fn_attempted, attempt,
                                      outcomes=dict(_pc.fn_outcomes))
            _outcomes = dict(_pc.fn_outcomes)
            _log(f"  [{module_name}/{_cluster_label}] targeted attempt {attempt} wrote test "
                 f"block(s) for {len(_pc.fn_attempted)} unexecuted function(s): "
                 + ", ".join(sorted(f"{site_label(s)} ({_outcomes.get(s, 'ran')})"
                                    for s in _pc.fn_attempted)))
        if _pc.fn_attempted or _pc.fn_verdicts:
            if callable(deps.save_fn_record):
                deps.save_fn_record(project_dir, _cluster_label, module_name, _fn_record)
        if _pc.kind == "return":
            assert _pc.result is not None
            return _pc.result
        if _pc.kind == "break":
            _at_ceiling = _pc.at_ceiling
            break

        # --- Graceful stop check ---
        # Routed through the shared, thread-safe check (already available as
        # deps.check_stop) rather than an inline sentinel read, so every
        # caller sees the same stop signal regardless of which thread or
        # loop first noticed it.
        deps.check_stop(f"{module_name} attempt {attempt}")

    # Behaviour-hardening pass — mutation-kill as the strength
    # metric on top of line coverage. Opt-in via TASK0C_MUTATION_HARDEN; a
    # no-op (returns 0) otherwise, so the default flow is unchanged.
    try:
        _rel_files = [
            str(p.relative_to(project_dir))
            for p in _iter_module_rs(module_dir)
            if not _is_gen_tests_file(p.name)
            and str(p.relative_to(project_dir)) not in existing_test_files
        ]
        _hard = _task0c_mutation_pass(project_dir, module_name, _current_cluster, _rel_files)
        if _hard:
            _log(f"  [{module_name}/{_cluster_label}] mutation-hardened {_hard} file(s).")
    except Exception as _me:
        _log(f"  [{module_name}/{_cluster_label}] mutation-harden pass skipped ({_me}).")

    _log(f"  [{module_name}/{_cluster_label}] Finished augmentation: {coverage:.1f}% coverage after {attempt} attempt(s).")
    return coverage, covered_lines, _at_ceiling, ""


def split_uncovered(existing_text: str, uncovered: Iterable[int], hits: Mapping[int, int],
                    proved_lines: frozenset[int], *, cluster: Any,
                    is_structural_line: Callable[[str], Any],
                    cfg_incompatible: Callable[..., Any],
                    cfg_excluded: Callable[..., Any],
                    spent_lines: frozenset[int] = frozenset()) -> UncoveredSplit:
    """Which of a file's uncovered lines a generation prompt is aimed at, and
    why each other one is not. The one definition of an actionable line: the
    generation pass targets `actionable`, and the per-file floor leaves
    `not_actionable` out of its denominator, so the stop never waits on a line
    no prompt will be aimed at, and reads `spent` (lines of a function whose
    targeted-attempt budget is spent) as accounted, so no prompt is aimed at a
    line the stop has stopped waiting on.

    `existing_text` is the file with its gen_tests declarations stripped (the
    view the prompt shows, numbered like the original source); a line past its
    end is dropped, as the pass always did."""
    lines = existing_text.splitlines()
    rest = {ln for ln in uncovered if ln <= existing_text.count("\n")}
    # LLVM/grcov instrumentation artifacts: comment-only lines (`// …`), which
    # grcov sometimes marks coverable, and structural-only tokens (`}`,
    # `} else {`), whose region counter stays 0 while the code around them
    # runs. Only when an adjacent line IS covered, confirming the block runs.
    artifact: set[int] = set()
    for ln in sorted(rest):
        if ln > len(lines):
            continue
        txt = lines[ln - 1].strip()
        if (txt.startswith("//") or (is_structural_line(txt) and bool(txt))) and any(
                hits.get(n, 0) > 0 for n in range(max(1, ln - 5), ln + 6) if n != ln):
            artifact.add(ln)
    rest -= artifact
    # Lines inside cfg blocks the cluster compiles out: incompatible with its
    # active cfg (`#[cfg(not(x))]` under a cluster that sets x), or excluded
    # from it (`#[cfg(x)]` under one that does not). No test here can run them;
    # no EXCL pragma either, since another cluster's pass covers them.
    incompat: set[int] = set()
    if cluster and cluster.active_cfg:
        incompat = set(cfg_incompatible(existing_text, rest, cluster.active_cfg) or ())
        rest -= incompat
    excluded: set[int] = set()
    if cluster and cluster.excluded_cfg:
        excluded = set(cfg_excluded(existing_text, rest, cluster.excluded_cfg) or ())
        rest -= excluded
    proved = rest & proved_lines
    spent = (rest - proved) & spent_lines
    return UncoveredSplit(frozenset(rest - proved - spent), frozenset(artifact),
                          frozenset(incompat), frozenset(excluded), frozenset(proved),
                          frozenset(spent))


def _line_filters(deps: Task0cDeps) -> dict[str, Any]:
    return {"is_structural_line": deps.is_structural_line,
            "cfg_incompatible": deps.filter_cfg_incompatible_lines,
            "cfg_excluded": deps.filter_cfg_excluded_lines}


def _read_file_floor(deps: Task0cDeps, project_dir: Path, module_dir: Path, cluster: Any,
                     files_data: Sequence[PerFileCoverage],
                     reading: FunctionExecution | str, record: FnCoverageRecord,
                     *, stalled: Iterable[str], floor: float) -> FileFloor:
    """The per-file floor over this measurement: the files the generation pass
    would queue (module source, not tests, not `build.rs`, not cfg-gated out
    of the cluster), each read over the lines a test can reach."""
    proved = reading.proved_targets if isinstance(reading, FunctionExecution) else ()
    spent = _spent_targets(reading, record)
    gated =cluster.gated_files if cluster is not None and cluster.gated_files else set()
    mod_prefix = str(module_dir) + "/"
    files: list[tuple[str, int, int, frozenset[int]]] = []
    not_actionable: dict[str, frozenset[int]] = {}
    for fdata in files_data:
        if not fdata.abs_path.startswith(mod_prefix):
            continue
        abs_path = Path(fdata.abs_path)
        rel = str(abs_path.relative_to(project_dir))
        if deps.is_test_file(abs_path, project_dir) or abs_path.name == "build.rs" or rel in gated:
            continue
        unc = frozenset(t["line"] for t in fdata.traces if t["stats"].get("Line", 1) == 0)
        if unc:
            try:
                raw = abs_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            view = deps.strip_gen_tests_decls(raw).rstrip() + "\n"
            hits = {t["line"]: t["stats"].get("Line", 0) for t in fdata.traces}
            split = split_uncovered(view, unc, hits,
                                    lines_in(t for t in proved if t.site.file == rel),
                                    cluster=cluster, **_line_filters(deps),
                                    spent_lines=lines_in(t for t in spent if t.site.file == rel))
            not_actionable[rel] = split.outside_denominator(unc)
        files.append((rel, fdata.covered, fdata.coverable, unc))
    return judge_files(floor, files, not_actionable=not_actionable, spent=spent, stalled=stalled)


def _spent_targets(reading: FunctionExecution | str,
                   record: FnCoverageRecord) -> tuple[FnTarget, ...]:
    """The unexecuted functions whose targeted-attempt budget is spent
    (`record.unreached`), with their spans from this reading: the set the floor
    reads as accounted and the generation pass holds out of every prompt."""
    if not isinstance(reading, FunctionExecution):
        return ()
    unreached = {u.site for u in record.unreached}
    return tuple(t for t in reading.unexecuted if t.site in unreached)


def _record_fn_reading(deps: Task0cDeps, project_dir: Path, module_name: str,
                       cluster_label: str, attempt: int,
                       record: FnCoverageRecord) -> None:
    """Persist the record after a reading, log it, emit it."""
    if callable(deps.save_fn_record):
        deps.save_fn_record(project_dir, cluster_label, module_name, record)
    log(f"  [{module_name}/{cluster_label}] {record.summary()}")
    emit("task0c.fn_coverage", data={
        "cluster": cluster_label, "module": module_name, "attempt": attempt,
        "measured": record.measured, "reason": record.reason,
        "candidates": record.candidates, "executed": len(record.executed),
        "unexecuted": [p.target.label for p in record.pending],
        "cluster_gated": record.gated_pending,
        "unreached": [site_label(u.site) for u in record.unreached],
        "unmeasured": len(record.unmeasured),
    })


def _behaviour_target_of(deps: Task0cDeps) -> float:
    """`deps.BEHAVIOUR_COVERAGE_TARGET` as a percent; anything unreadable as a
    number (a stub in a fakes-built deps) is 0 = off."""
    try:
        return max(0.0, float(deps.BEHAVIOUR_COVERAGE_TARGET))
    except (TypeError, ValueError):
        return 0.0


def _file_floor_of(deps: Task0cDeps) -> float:
    """`deps.COVERAGE_FILE_FLOOR` as a percent; anything unreadable as a number
    (a stub in a fakes-built deps) is 0 = off."""
    try:
        return max(0.0, float(deps.COVERAGE_FILE_FLOOR))
    except (TypeError, ValueError):
        return 0.0


def _load_behaviour_specs(
    deps: Task0cDeps, project_dir: Path, cluster_name: str,
    log: Callable[[str], None] | None = None,
) -> tuple[Any, ...]:
    """The cluster's harvested behaviour specs, () when there is no suite. The
    suite is an additive oracle: a loader that fails must never break a run."""
    loader = deps.load_behaviour_suite
    if loader is None:
        return ()
    try:
        suite = loader(project_dir, cluster_name)
    except Exception as exc:
        if log:
            log(f"  [task0c] behaviour suite for {cluster_name} unreadable ({exc}) — none listed.")
        return ()
    specs = getattr(suite, "specs", None)
    return tuple(specs) if isinstance(specs, (tuple, list)) else ()


def task0c_behaviour_coverage(
    project_dir: Path, module_name: str, module_dir: Path, deps: Task0cDeps,
) -> tuple[int, int]:
    """`(pinned, total)`: of the behaviours THIS TASK is asked to pin, how many
    a pipeline-generated test of the module pins exactly — leftover
    `*.gen_tests_*.rs` siblings or `tests/{stem}_gen_{N}.rs` integration files.

    The denominator is the pinnable specs MINUS the capture-owned ones, for the
    same reason the per-file listing drops them: with the split on, this task is
    neither shown those specs nor held to them, so counting them would make the
    ratio unreachable by construction and `_behaviour_rounds` would spend a
    round (and a `test_harness_compiles` build) per module per cluster
    discovering that, then log `stopping below target` — a permanent red
    reading describing a deliberate design choice. A measurement whose meaning
    changes has to be told; the exclusion is logged rather than silently
    folded in."""
    cluster = deps.get_current_cluster()
    cluster_name = cluster.name if cluster else "stable"
    all_pinnable = pinnable_specs(_load_behaviour_specs(deps, project_dir, cluster_name))
    specs = drop_capture_owned(all_pinnable, enabled=bool(deps.BEHAVIOUR_CAPTURE_SPLIT))
    if len(specs) != len(all_pinnable):
        log(f"  [{module_name}/{cluster_name}] behaviour coverage denominator: "
            f"{len(specs)} of {len(all_pinnable)} pinnable spec(s) — "
            f"{len(all_pinnable) - len(specs)} are capture-owned and this task "
            f"is not asked to pin them.")
    if not specs:
        return 0, 0
    pinned: set[str] = set()
    for p in _iter_module_rs(module_dir):
        if not deps.is_gen_tests_file(p.name):
            continue
        with suppress(OSError):
            pinned |= behaviours_pinned_by(p.read_text(encoding="utf-8"), specs)
    return len(pinned), len(specs)


def task0c_behaviour_pass(
    project_dir: Path, module_name: str, module_dir: Path, deps: Task0cDeps, *,
    project_context: str = "", coverage_strategy: str = "",
) -> bool:
    """One generation pass over the module's files that still have unpinned
    harvested behaviours, through the per-file Phase C entry with the
    behaviours as the target: the "uncovered" lines are the signature lines of
    the fns the behaviours exercise (every fn of the file when `exercises` is
    empty), so fn_bodies shows the model the fns to drive and the admission
    site holds each block to the pin contract. Returns True when at least one
    block was admitted."""
    _log = log
    cluster = deps.get_current_cluster()
    cluster_name = cluster.name if cluster else "stable"
    target = _behaviour_target_of(deps)
    specs = _load_behaviour_specs(deps, project_dir, cluster_name, log=_log)
    # The same filter the listing and the coverage denominator use: without it
    # a suite whose every pinnable spec is capture-owned still bought a
    # `test_harness_compiles` cargo build before the per-file loop discovered
    # there was nothing to ask for.
    if target <= 0 or not drop_capture_owned(
            pinnable_specs(specs), enabled=bool(deps.BEHAVIOUR_CAPTURE_SPLIT)):
        return False
    augmentor = deps.test_augmentor_factory()
    harness_ok, harness_err = augmentor.test_harness_compiles(project_dir, module_name)
    if not harness_ok:
        _log(f"  [{module_name}/{cluster_name}] behaviour pass skipped — the test harness does "
             f"not compile: {harness_err[:300]}")
        return False
    gated = set(cluster.gated_files or ()) if cluster and cluster.gated_files else set()
    files_data: list[PerFileCoverage] = []
    for src in _iter_module_rs(module_dir):
        if deps.is_test_file(src, project_dir) or deps.is_gen_tests_file(src.name) \
                or src.name == "build.rs":
            continue
        rel = str(src.relative_to(project_dir))
        if rel in gated:
            continue
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError:
            continue
        text = deps.strip_gen_tests_decls(raw).rstrip() + "\n"
        items = declared_items(text)
        fn_names = {it.name for it in items if it.kind == "fn"}
        # Filtered the same way the per-file listing is. Without this the
        # behaviour pass still SCHEDULES a whole generation round over files
        # whose only unpinned specs are capture-owned — the inner listing is
        # then empty and the round is a plain coverage pass bought at the price
        # of a behaviour pass.
        unpinned = drop_capture_owned(
            select_behaviours_for_file(
                specs, file_fn_names=fn_names,
                file_pub_items=public_item_names(items),
                already_pinned=behaviours_pinned_by(
                    deps.read_all_gen_tests(src), specs)),
            enabled=bool(deps.BEHAVIOUR_CAPTURE_SPLIT))
        if not unpinned:
            continue
        spans = fn_line_spans(text)
        exercised = {seg for s in unpinned for e in s.exercises for seg in e.split("::")} & fn_names
        targets = {spans[n][0] for n in (exercised or fn_names) if n in spans}
        if not targets:
            continue
        _log(f"  [{module_name}/{cluster_name}] behaviour pass: {rel} — {len(unpinned)} "
             f"unpinned behaviour(s), {len(targets)} fn(s) targeted")
        files_data.append(PerFileCoverage(
            abs_path=str(src), path=list(src.parts),
            traces=[{"line": n, "stats": {"Line": 0}} for n in sorted(targets)],
            covered=0, coverable=len(targets)))
    if not files_data:
        _log(f"  [{module_name}/{cluster_name}] behaviour pass: no file has an unpinned "
             f"behaviour this task is asked to pin"
             f"{' (capture-owned classes excluded)' if deps.BEHAVIOUR_CAPTURE_SPLIT else ''}.")
        return False
    allow_line, prelude_block = deps.derive_gentest_prelude(project_dir)
    outcome = _run_phase_c(
        project_dir=project_dir, module_name=module_name, module_dir=module_dir,
        project_context=project_context, coverage_strategy=coverage_strategy,
        attempt=1, coverage=0.0, covered_lines=0, files_data=files_data,
        hist=Task0cFileHistory(),
        ctx=_PhaseCCtx(
            deps=deps, current_cluster=cluster, cluster_label=cluster_name, split_n=1,
            allow_line=allow_line, prelude_block=prelude_block,
            prelude_names=deps.prelude_import_names(prelude_block),
            file_stall_limit=1, retro_deep_at_stall=1, stall_context=False,
            behaviour_pass=True),
    )
    _log(f"  [{module_name}/{cluster_name}] behaviour pass: {outcome.blocks_admitted} block(s) admitted.")
    return outcome.blocks_admitted > 0


def select_relevant_siblings(
    target_rel: str,
    target_source: str,
    all_siblings: list[tuple[str, str]],
    project_dir: Path,
) -> list[tuple[str, str]]:
    """
    Python type-name overlap ranking — replaces the BL-014 LLM planner call.

    Algorithm:
    1. Extract CamelCase identifiers used in the target file (types, structs, etc.)
    2. For each sibling, count how many of those identifiers are *defined* there
       (defined = appears after struct/enum/trait/type/impl keyword)
    3. Return all siblings sorted by overlap count descending — the budget system
       decides how many to include.

    Zero LLM calls, deterministic, and empirically better for type-heavy files
    (e.g. error.rs uses Own/Mut/Ref → correctly ranks ptr.rs first).
    """
    if not all_siblings:
        return []

    # CamelCase type-name + `kw Name` definition scans → pure tokeniser
    # (byte-equivalent), no regex on source.
    used_types: set[str] = camelcase_type_names(target_source)

    def _overlap(content: str) -> int:
        return len(used_types & defined_type_names(content))

    scored = sorted(
        ((srel, sc, _overlap(sc)) for srel, sc in all_siblings),
        key=lambda x: x[2],
        reverse=True,
    )

    top = [(srel, score) for srel, _sc, score in scored if score > 0][:4]
    log(f"  [task0c planner] {target_rel}: ranked {len(all_siblings)} siblings by type overlap"
         f" (top: {top if top else 'none matched'})")
    emit("task0c.planner", file=target_rel)  # file-progress source for the monitor
    return [(srel, sc) for srel, sc, _score in scored]
