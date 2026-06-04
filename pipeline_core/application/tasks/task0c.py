"""TASK 0c — per-module coverage augmentation engine (extracted, slice 5b).

The 1316-line body is moved VERBATIM from pipeline.py; the only addition is a prelude that
rebinds every collaborator as a local from the injected :class:`Task0cDeps`, so the body is
byte-for-byte unchanged (zero substitution risk). All regex was factored into named pipeline
helpers before the move (they are injected). subprocess/git I/O is injected (the module never
imports subprocess); `_current_cluster` is resolved at call time via a getter. No import of
pipeline.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.refine import Attempt, LoopState
from ...log import emit, log


@dataclass(frozen=True, slots=True)
class Task0cDeps:
    BUILD_TIMEOUT: Any
    COVERAGE_TARGET: Any
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
    build_task0c_prompt_sections: Any
    cargo_env: Any
    cluster_cfg_attr: Any
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
    gen_tests_count: Any
    gen_tests_path: Any
    get_task0c_history: Any
    has_nonempty_test_body: Any
    inject_allow_before_any_mod: Any
    inject_allow_before_gen_mod: Any
    inject_gen_prelude_imports: Any
    is_gen_tests_file: Any
    is_structural_line: Any
    load_file_constraints: Any
    log_task0c_attempt: Any
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
    task0c_mutation_pass: Any
    task0c_pragma_pass: Any
    task0c_retrospective: Any
    task0c_targeted_pragma: Any
    test_augmentor_factory: Any
    ask: Any
    extract_files: Any
    extract_test_mod: Any
    generate_fix_loop: Any
    generate_in_parallel: Any
    is_test_file: Any
    write: Any
    subprocess: Any
    get_current_cluster: Any
    coverage_provider: Any


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    deps: Task0cDeps,
    crate_type: str = "",
    existing_test_files: set | None = None,
    project_context: str = "",
    coverage_strategy: str = "",
) -> tuple[float, int, bool]:
    """
    Generate unit tests (#[cfg(test)] blocks appended inline) for a module
    until tarpaulin reports 100% coverage or MAX_RETRIES is exhausted.
    Returns final coverage percentage.
    """
    # slice 5b: rebind injected collaborators as locals so the body below is verbatim.
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
    _build_task0c_prompt_sections = deps.build_task0c_prompt_sections
    _cargo_env = deps.cargo_env
    _cluster_cfg_attr = deps.cluster_cfg_attr
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
    _gen_tests_count = deps.gen_tests_count
    _gen_tests_path = deps.gen_tests_path
    _get_task0c_history = deps.get_task0c_history
    _has_nonempty_test_body = deps.has_nonempty_test_body
    _inject_allow_before_any_mod = deps.inject_allow_before_any_mod
    _inject_allow_before_gen_mod = deps.inject_allow_before_gen_mod
    _inject_gen_prelude_imports = deps.inject_gen_prelude_imports
    _is_gen_tests_file = deps.is_gen_tests_file
    _is_structural_line = deps.is_structural_line
    _load_file_constraints = deps.load_file_constraints
    _log_task0c_attempt = deps.log_task0c_attempt
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
    _task0c_mutation_pass = deps.task0c_mutation_pass
    _task0c_pragma_pass = deps.task0c_pragma_pass
    _task0c_retrospective = deps.task0c_retrospective
    _task0c_targeted_pragma = deps.task0c_targeted_pragma
    _test_augmentor_factory = deps.test_augmentor_factory
    ask = deps.ask
    extract_files = deps.extract_files
    extract_test_mod = deps.extract_test_mod
    generate_fix_loop = deps.generate_fix_loop
    generate_in_parallel = deps.generate_in_parallel
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

    # TASK0C_SPLIT_N: split-and-merge degree for per-file generation. 1 = legacy
    # single-shot. >1 = partition the file's uncovered functions into N disjoint
    # subsets, generate a test block for each CONCURRENTLY (shared-prefix prompts
    # batch on the GPU), then commit each block through the normal compile-fix
    # loop so survivors accumulate (additive coverage). Generation is parallel;
    # every cargo/coverage call stays serial (no shared-target/ race). Coverage
    # (quick-cov) runs once on the merged result. See _partition_uncovered_lines.
    _split_n = max(1, int(os.getenv("TASK0C_SPLIT_N", "1")))
    if _split_n > 1:
        _log(f"  [{module_name}/{_cluster_label}] split-and-merge generation enabled: "
             f"TASK0C_SPLIT_N={_split_n} (subset blocks concurrent; compile+cov serial).")

    augmentor = _test_augmentor_factory()

    if not augmentor.is_applicable(crate_type):
        _log(f"  [{module_name}] crate type '{crate_type}' not applicable for augmentation — skipping.")
        return 100.0, 0, False  # treat as done

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
            for src_file in module_dir.rglob("*.rs")
            if not is_test_file(src_file, project_dir)
        )
        if not _lib_err_in_gen:
            # Check whether any of the broken files are writable (i.e. we wrote them).
            # If the error is only in write-protected originals, we truly cannot fix it.
            _broken_in_err = [
                src_file for src_file in module_dir.rglob("*.rs")
                if (str(src_file.relative_to(project_dir)) in lib_err
                    or src_file.name in lib_err)
                and not is_test_file(src_file, project_dir)
                and os.access(src_file, os.W_OK)
            ]
            if not _broken_in_err:
                err_snippet = lib_err[:400]
                _log(f"  [{module_name}] Library does not compile in write-protected files — skipping.")
                _log(f"    Error: {err_snippet}")
                return 0.0, 0, False
            _log(f"  [{module_name}] Library compile failure in writable source — routing to harness fix loop.")
        else:
            _log(f"  [{module_name}] Library compile failure in gen_* block — routing to harness fix loop.")

    def _error_signatures(cargo_output: str) -> frozenset:
        """Extract stable error signatures — error code + message, no file/line numbers."""
        return _error_signatures_from_output(cargo_output)  # type: ignore[no-any-return]

    harness_ok, compile_err_out = augmentor.test_harness_compiles(project_dir, module_name)
    if not harness_ok:
        compile_err = compile_err_out
        prev_signatures: frozenset = frozenset()
        fix_attempt = 0

        while True:
            fix_attempt += 1
            _log(f"  [{module_name}] Harness fix attempt {fix_attempt} — sending compile error to LLM.")

            # Find source files mentioned in the error that belong to this module.
            broken_files: list[Path] = []
            for src_file in sorted(module_dir.rglob("*.rs")):
                rel = str(src_file.relative_to(project_dir))
                if rel in compile_err or src_file.name in compile_err:
                    broken_files.append(src_file)
            if not broken_files:
                for src_file in sorted(module_dir.rglob("*.rs")):
                    try:
                        if "#[cfg(test)]" in src_file.read_text(encoding="utf-8"):
                            broken_files.append(src_file)
                    except Exception:
                        pass

            if not broken_files:
                _log(f"  [{module_name}] Cannot identify broken file — skipping augmentation.")
                return 0.0, 0, False

            # Pass 1: ask LLM to fix each broken file — preserve as much generated
            # test code as possible rather than discarding it immediately.
            for src_file in broken_files:
                if is_test_file(src_file, project_dir):
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
                files = extract_files(response)
                if rel in files:
                    write(project_dir / rel, files[rel])
                    _log(f"    LLM fixed test code in {rel}")
                else:
                    _log(f"    LLM produced no output for {rel}")

            harness_ok, compile_err_out = augmentor.test_harness_compiles(project_dir, module_name)
            if harness_ok:
                _log(f"  [{module_name}] Harness fixed by LLM after {fix_attempt} attempt(s).")
                break

            new_signatures = _error_signatures(compile_err_out)
            resolved = prev_signatures - new_signatures
            _log(f"  [{module_name}] Still broken — {len(new_signatures)} error signature(s), "
                 f"{len(resolved)} resolved since last attempt.")

            if new_signatures == prev_signatures:
                # Truly stuck — LLM made no progress.
                # Targeted strip: remove only the specific gen_* block(s) whose line
                # ranges overlap with error lines — preserving all other gen_* blocks.
                _log(f"  [{module_name}] No error improvement — targeted strip of broken gen_* block(s).")

                def _strip_broken_gen_blocks(src_file: Path, error_text: str) -> bool:
                    """Remove gen_tests artifacts that cover broken code. Returns True if changed.

                    With the sibling-file design, each `foo.gen_tests_N.rs` IS one block.
                    If src_file is a sibling, delete it and remove its parent's decl.
                    If src_file is a real source file, strip all its gen_tests artifacts
                    (siblings + decls) — we can no longer target an individual block within
                    a source file because none live there anymore.
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
                    # Real source file — strip all gen_tests artifacts.
                    n = _strip_all_gen_tests_artifacts(src_file)
                    if n > 0:
                        _log(f"  [{module_name}] Stripped {n} gen_tests sibling(s) for "
                             f"{src_file.relative_to(project_dir)}.")
                        return True
                    return False

                any_stripped = False
                for src_file in broken_files:
                    if is_test_file(src_file, project_dir):
                        continue
                    if _strip_broken_gen_blocks(src_file, compile_err_out):
                        any_stripped = True

                harness_ok, _broken_err = augmentor.test_harness_compiles(project_dir, module_name)
                if not harness_ok and any_stripped:
                    # Targeted strip removed some blocks but harness is still broken.
                    # Fall through to a full strip of ALL gen_tests artifacts from broken files.
                    _log(f"  [{module_name}] Targeted strip insufficient — doing full gen_tests strip.")
                    for src_file in broken_files:
                        if is_test_file(src_file, project_dir):
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
                        for _sf in sorted(module_dir.rglob("*.rs")):
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
                                break
                    _log(f"  [{module_name}] Harness still broken after targeted strip — skipping augmentation.")
                    return 0.0, 0, False
                _log(f"  [{module_name}] Harness fixed by targeted strip.")
                break

            prev_signatures = new_signatures
            compile_err = compile_err_out

    # Derive allow attributes and prelude imports from project source — no LLM.
    _allow_line, _prelude_block = _derive_gentest_prelude(project_dir)
    _prelude_names = _prelude_import_names(_prelude_block)

    # Pre-clean only if the harness is broken — if existing gen_tests compile,
    # keep them so accumulated coverage is never lost on re-entry.
    _precheck_ok, _ = augmentor.test_harness_compiles(project_dir, module_name)
    if not _precheck_ok:
        for src_file in sorted(module_dir.rglob("*.rs")):
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

    # P7.7: floor-stall semantics with the absolute cap raised so it's no
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
    files_data: list[dict] = []
    _skip_first_measure = False
    # On resume, reload coverage from the previous run's grcov JSON + filecache
    # to avoid an expensive full re-run before the first gen attempt.
    if attempt > 0:
        _cached = _coverage_provider.load_cached(project_dir, module_name)
        if _cached is not None:
            coverage, files_data, covered_lines, coverable_lines = _cached
            _skip_first_measure = True
            _log(f"  [{module_name}/{_cluster_label}] Loaded coverage from previous run: "
                 f"{coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
    prev_covered = covered_lines if _skip_first_measure else -1
    no_improvement_count = 0
    MAX_NO_IMPROVEMENT = int(os.getenv(
        "TASK0C_MAX_STALLS", "5"
    ))  # P7.7: tightened — per-file retrospective fires first; this is the
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
    file_stall_counts: dict[str, int] = {}   # consecutive no-improvement attempts per file
    file_best_uncov: dict[str, int] = {}     # fewest uncovered lines seen per file
    file_prev_uncov_lines: dict[str, set] = {}  # uncov_lines snapshot from previous attempt per file
    skipped_files: set[str] = set()          # files permanently skipped due to stall
    file_what_worked: dict[str, str] = {}    # confirmed-working patterns per file (from improving attempts)
    file_retrospectives: dict[str, str] = {} # per-file classification of why coverage stalled
    # Track concrete attempts that compiled but added zero coverage to the
    # targeted lines. Fed to the deep retrospective so the classifier can see
    # which strategies the LLM has already tried — and revise its TESTABLE
    # call to UNREACHABLE when evidence demands it.
    file_last_qcov: dict[str, int] = {}          # last quick-cov 'covered' count per file
    file_failed_blocks: dict[str, list[str]] = {}  # gen blocks that compiled but didn't move the metric
    file_giveup_counts: dict[str, int] = {}      # consecutive attempts the LLM flagged "unreachable"
    file_loops: dict[str, LoopState] = {}        # per-file refine history → meta-awareness context
    # FIX (session 48): give the generation model META-AWARENESS that it is stalling (today it gets
    # none — it re-tries blind), and FEED the consecutive give-up signal to the retrospective so
    # genuinely-unreachable lines are classified (and pragma'd) faster instead of burning the full
    # stall budget. Env-gated so a fresh run can be A/B'd against the current (pre-fix) run.
    _STALL_CONTEXT = os.getenv("TASK0C_STALL_CONTEXT", "1") == "1"
    _at_ceiling = False                      # True when a full pass produced no actionable lines
    while attempt < max_attempts:
        attempt += 1
        _log(f"  [{module_name}/{_cluster_label}] Attempt {attempt}/{max_attempts}")

        # --- Phase A: compile check + targeted fix ---
        # Use generate_fix_loop: run compile check, if it fails ask LLM to fix
        # specific files rather than regenerating from scratch.
        # Only source files with #[cfg(test)] blocks are candidates — these hold
        # the generated tests that may have syntax errors.
        module_source_paths = [
            f for f in sorted(module_dir.rglob("*.rs"))
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
            coverage, files_data, covered_lines, coverable_lines = augmentor.measure_coverage(project_dir, module_name, module_dir)
            _log(f"  [{module_name}/{_cluster_label}] Coverage: {coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
        _mark_phase(project_dir, f"task0c/{_cluster_label}/{module_name}", "in_progress",
                    detail=f"{coverage:.1f}% attempt {attempt}", source_hash=_source_hash(project_dir))
        _set_current(project_dir, f"task0c/{_cluster_label}/{module_name} / {coverage:.1f}% attempt {attempt}")

        if coverage >= _COVERAGE_TARGET:
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
            _log(f"  [{module_name}] Coverage regression {delta_lines:+d} lines — running tests to detect runtime failures.")
            _reg_env = _cargo_env()
            _reg_result = subprocess.run(
                ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1", "--no-capture"],
                cwd=project_dir, capture_output=True, text=True,
                timeout=BUILD_TIMEOUT, env=_reg_env,
            )
            if _reg_result.returncode != 0:
                _reg_output = (_reg_result.stdout + "\n" + _reg_result.stderr)[-_FIX_ERR_CAP:]
                _log(f"  [{module_name}] Test failures confirmed — running runtime fix loop.")
                generate_fix_loop(
                    project_dir=project_dir,
                    check_cmd=f"cargo test -p {module_name} -- --test-threads=1",
                    source_paths=module_source_paths,
                    existing_test_files=existing_test_files,
                    model=_TASK0C_MODEL,
                    max_fix_attempts=3,
                    timeout=BUILD_TIMEOUT,
                )
            else:
                _log(f"  [{module_name}] Tests pass but coverage regressed — likely stale profraw; retrying.")
            prev_covered = covered_lines
            continue  # skip Phase C (no new tests until runtime failures are resolved)

        # P7.7: uniform loop.iter event on every attempt for monitor.py.
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
                # loop will keep trying (or the main loop's FR-018 guard fires).
                break
        else:
            no_improvement_count = 0
        prev_covered = covered_lines


        # --- Phase C: generate/augment tests for uncovered lines ---
        # Filter to source files in this module with uncovered lines.
        # Skip test files — adding tests inside test files causes circular issues.
        mod_prefix = str(module_dir) + "/"
        uncovered_by_file: dict[str, tuple[str, set[int]]] = {}
        for fdata in files_data:
            abs_path_str = fdata["abs_path"] if "abs_path" in fdata else os.sep.join(fdata["path"])
            if not abs_path_str.startswith(mod_prefix):
                continue
            rel = str(Path(abs_path_str).relative_to(project_dir))
            lines_uncov = {
                t["line"] for t in fdata.get("traces", [])
                if t["stats"].get("Line", 1) == 0
            }
            _gated = _current_cluster.gated_files if _current_cluster and _current_cluster.gated_files else set()
            if lines_uncov and not is_test_file(Path(abs_path_str), project_dir) \
                    and Path(abs_path_str).name != "build.rs" \
                    and rel not in _gated:
                uncovered_by_file[rel] = (fdata.get("content", ""), lines_uncov)
                # Baseline-floor seed: the file's covered-line count from the
                # start-of-attempt full measurement. The quick-cov accept gate
                # (below) reverts any block whose quick-cov drops at/below this,
                # so a DESTRUCTIVE first block (e.g. a test that aborts the test
                # binary and wipes real coverage — fmt.rs 84→40) is caught instead
                # of sailing through on a None _prev_qcov. Without this seed the
                # gate only guards the 2nd+ block against the 1st, never the 1st
                # against baseline, letting a net-negative block lower total
                # coverage and spin the outer attempt loop.
                file_last_qcov[rel] = fdata.get("covered", 0)

        if not uncovered_by_file:
            _log(f"  [{module_name}] No uncovered lines in module source files.")
            break

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
        _target_dir = project_dir / "target"
        for sib_file in sorted(module_dir.rglob("*.rs")):
            if is_test_file(sib_file, project_dir):
                continue
            if sib_file.name == "build.rs":
                continue  # build scripts are not library code — no useful type context
            try:
                if sib_file.is_relative_to(_target_dir):
                    continue  # skip build artifacts
            except Exception:
                pass
            try:
                sib_rel = str(sib_file.relative_to(project_dir))
                sib_text = sib_file.read_text(encoding="utf-8")
                sibling_files_all.append((sib_rel, sib_text))
            except Exception:
                pass

        _file_order_strategy = os.getenv("TASK0C_FILE_ORDER", "easy_first").strip().lower()
        if _file_order_strategy == "easy_first":
            _ordered_files = sorted(
                uncovered_by_file.items(),
                key=lambda kv: (file_stall_counts.get(kv[0], 0), len(kv[1][1]))
            )
        else:
            _ordered_files = list(uncovered_by_file.items())

        _queue_summary = ", ".join(
            f"{rel.split('/')[-1]}({len(unc)}raw{'*' if file_stall_counts.get(rel, 0) > 0 else ''})"
            for rel, (_, unc) in _ordered_files
            if rel not in skipped_files
        )
        _log(f"  [{module_name}] Queue: {len([r for r,_ in _ordered_files if r not in skipped_files])} file(s) — {_queue_summary} (raw=pre-filter grcov count)")

        _active_files = [r for r, _ in _ordered_files if r not in skipped_files]
        attempt_n_within_outer = 0
        _pass_had_work = False   # set True if any file makes a real LLM call this pass
        for rel_path, (content, uncov_lines) in _ordered_files:
            attempt_n_within_outer += 1

            if rel_path in skipped_files:
                _log(f"  [{module_name}] Skipping {rel_path} — stalled for {FILE_STALL_LIMIT} consecutive attempts.")
                continue

            _file_idx = _active_files.index(rel_path) + 1 if rel_path in _active_files else attempt_n_within_outer
            _log(f"  [{module_name}] File {_file_idx}/{len(_active_files)}: {rel_path} ({len(uncov_lines)} raw uncovered lines)")

            abs_path = project_dir / rel_path
            # _raw_text = current on-disk content (including existing gen_tests decls).
            # This is the revert target: on a failed pass we restore _raw_text and delete
            # the just-written sibling, so previously-committed passes survive intact.
            _raw_text = abs_path.read_text(encoding="utf-8") if abs_path.exists() else content
            # gen_tests bodies live in sibling files. Read them all (concatenated as
            # `mod gen_tests_N { ... }` blocks) so the LLM sees what it generated
            # before and can extend rather than restart.
            _old_block = _read_all_gen_tests(abs_path)
            # The view shown to the LLM (and used for uncov_lines line counting) strips
            # the path-mod declaration lines so the line numbers match the original src.
            existing_text = _strip_gen_tests_decls(_raw_text).rstrip() + "\n"

            _src_line_count = existing_text.count('\n')
            uncov_lines = {ln for ln in uncov_lines if ln <= _src_line_count}

            # Filter LLVM/grcov instrumentation artifacts:
            # 1. Comment-only lines (// …) — grcov sometimes marks these coverable, they never are
            # 2. Structural-only tokens (}, } else {) — LLVM region counter stays 0 even when
            #    the surrounding code executes (known grcov/LLVM quirk)
            # Only strip if at least one adjacent line IS covered (confirming the block runs).
            _file_cov_hits: dict[int, int] = {}
            for _fd in files_data:
                _fd_abs = _fd.get("abs_path", os.sep.join(_fd.get("path", [])))
                if _fd_abs == str(abs_path):
                    _file_cov_hits = {t["line"]: t["stats"].get("Line", 0) for t in _fd.get("traces", [])}
                    break
            _src_lines_list = existing_text.splitlines()
            _artifact_lines: set[int] = set()
            for _al in sorted(uncov_lines):
                if _al > len(_src_lines_list):
                    continue
                _al_txt = _src_lines_list[_al - 1].strip()
                _is_comment    = _al_txt.startswith("//")
                _is_structural = _is_structural_line(_al_txt) and bool(_al_txt)
                if _is_comment or _is_structural:
                    # Confirm by checking that at least one adjacent line is covered
                    if any(_file_cov_hits.get(n, 0) > 0 for n in range(max(1, _al - 5), _al + 6) if n != _al):
                        _artifact_lines.add(_al)
            if _artifact_lines:
                _log(f"  [{module_name}] {rel_path} — filtered {len(_artifact_lines)} LLVM artifact line(s): {sorted(_artifact_lines)}")
                uncov_lines -= _artifact_lines

            # Filter lines inside cfg blocks incompatible with the current cluster
            # (e.g. #[cfg(not(error_generic_member_access))] under the nightly cluster).
            # These lines don't compile here — no test can cover them. Filter only, no EXCL.
            if _current_cluster and _current_cluster.active_cfg:
                _incompat = _filter_cfg_incompatible_lines(
                    existing_text, uncov_lines, _current_cluster.active_cfg
                )
                if _incompat:
                    _log(f"  [{module_name}] {rel_path} — filtered {len(_incompat)} cfg-incompatible line(s) "
                         f"(compiled out under {_current_cluster.name}): {sorted(_incompat)}")
                    uncov_lines -= _incompat

            # Filter lines inside cfg blocks that are excluded in this cluster
            # (e.g. #[cfg(error_generic_member_access)] under the stable cluster).
            # No EXCL pragmas written — these lines ARE coverable in the nightly cluster
            # pass and must not be permanently marked ignored.
            if _current_cluster and _current_cluster.excluded_cfg:
                _cfg_excl = _filter_cfg_excluded_lines(
                    existing_text, uncov_lines, _current_cluster.excluded_cfg
                )
                if _cfg_excl:
                    _log(f"  [{module_name}] {rel_path} — filtered {len(_cfg_excl)} excluded-cfg line(s) "
                         f"(compiled out in {_current_cluster.name}): {sorted(_cfg_excl)}")
                    uncov_lines -= _cfg_excl

            if not uncov_lines:
                _log(f"  [{module_name}] {rel_path} — no actionable uncovered lines after filtering, skipping.")
                continue

            _log(f"  [{module_name}] {rel_path} — {len(uncov_lines)} actionable uncovered lines (post-filter)")
            fn_bodies = _extract_uncovered_functions(existing_text, uncov_lines)
            pub_symbols = _extract_public_symbols(existing_text)

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
            mod_name = f"gen_tests_{_pass_n}"
            output_format = (
                f'<FILE path="gen_tests_block">\n'
                f'#[cfg(test)]\n{_allow_line}\nmod {mod_name} {{{{\n'
                f'    use super::*;\n'
                f'    {_prelude_block}\n\n'
                f'    #[test]\n    fn test_example() {{{{\n        // tests\n    }}}}\n}}}}\n'
                f'</FILE>'
            )
            _coverage_status_probe = (
                f"\nCoverage status: {coverage:.1f}% covered. "
                f"The {len(uncov_lines)} line(s) in fn_bodies below are the ONLY remaining uncovered targets — "
                f"all other lines are already covered. Focus exclusively on these.\n"
            ) if coverage > 0 else ""
            _env_section = _describe_test_environment()
            _file_constraints = _load_file_constraints(project_dir)
            _target_basename = Path(rel_path).name
            _file_constraints_section = _file_constraints.get(_target_basename, "")
            _scaffold_template = f"""You are writing Rust unit tests that specify the behavioral contract of source code.
{_TASK0C_THINKING_DIRECTIVE}{_coverage_status_probe}
Target file: {rel_path}
{_env_section}
Write a `#[cfg(test)] mod {mod_name}` block for `{rel_path}`.
Write a new test block covering the uncovered lines shown in fn_bodies above.

Coverage guidance:
- Prefer tests with at least one `assert!`, `assert_eq!`, `assert_ne!`, or `.unwrap()` —
  asserting on return values adds spec value. If asserting is genuinely awkward (void
  returns, side effects only), simply exercising the code path is acceptable.
- Prefer testing edge cases and error conditions, not just happy paths.
- When testing a function that returns `Result` or `Option`, test BOTH the Ok/Some path
  AND the Err/None path if the code shows both branches.

Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access module items.
- Read field types from the struct definitions in the source file — match EXACTLY.

INTERNAL MODULE RULES — these tests live inside the source file, not in an external test crate:
- Do NOT write `use <crate_name>::...` — you are already inside the crate.
- The template prelude already imports: {_prelude_names} — use them directly.
- Do NOT use `#[macro_export]` macros inside this module — they cannot be imported from within the crate.
  Construct values through direct API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
{_nightly_rules_for_cluster()}
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

            def _render_prompt(_secs: dict, _cov_status: str) -> str:
                return f"""You are writing Rust unit tests that specify the behavioral contract of source code.
{_TASK0C_THINKING_DIRECTIVE}{_secs['strategy']}{_secs['history']}{_secs['what_worked']}{_secs['retrospective']}{_secs['context']}{_cov_status}
Target file: {rel_path}
{_env_section}{_secs['cargo']}{_secs['full_source']}{_secs['fn_bodies']}{_secs['symbols']}{_secs['examples']}{_secs['integ_tests']}{_secs['siblings']}{_secs['old_block']}
Write a `#[cfg(test)] mod {mod_name}` block for `{rel_path}`.
{"Write ONLY new test functions for the uncovered lines in fn_bodies above. The existing test module(s) shown are already on disk — do NOT copy or repeat any test from them." if _secs['old_block'] else "Write a new test block covering the uncovered lines shown in fn_bodies above."}

Coverage guidance:
- Prefer tests with at least one `assert!`, `assert_eq!`, `assert_ne!`, or `.unwrap()` —
  asserting on return values adds spec value. If asserting is genuinely awkward (void
  returns, side effects only), simply exercising the code path is acceptable.
- Prefer testing edge cases and error conditions, not just happy paths.
- When testing a function that returns `Result` or `Option`, test BOTH the Ok/Some path
  AND the Err/None path if the code shows both branches.

Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access module items.
- Read field types from the struct definitions in the source file — match EXACTLY.

INTERNAL MODULE RULES — these tests live inside the source file, not in an external test crate:
- Do NOT write `use <crate_name>::...` — you are already inside the crate.
- The template prelude already imports: {_prelude_names} — use them directly.
- Do NOT use `#[macro_export]` macros inside this module — they cannot be imported from within the crate.
  Construct values through direct API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
{_nightly_rules_for_cluster()}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}
{output_format}
"""

            prompt = _render_prompt(secs, _coverage_status)

            def _subset_prompt(_subset_lines: set[int]) -> str:
                """Render a generation prompt targeting only a subset of the
                file's uncovered lines (split-and-merge). Same file context as
                the full prompt; only the fn_bodies/target section narrows, so
                the big prefix is shared across the concurrent subset requests."""
                _sfn = _extract_uncovered_functions(existing_text, _subset_lines)
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
                    scaffold_chars=len(_scaffold_template),
                )
                return _render_prompt(_ssecs, _coverage_status_probe)

            # FR-012: retry generation — qwen3 thinking mode sometimes returns empty output.
            # On retry, send a short explicit follow-up instead of the full prompt again:
            # repeating the same large prompt produces the same empty result.
            def _save_stream_log(gen_attempt: int, query: str, resp: str, reason: str) -> None:
                _fname = rel_path.replace("/", "_").replace(".", "_")
                _path = Path(f"/tmp/stream_loop_{attempt}_{_fname}_gen{gen_attempt}.log")
                try:
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

            def _finalize_and_commit(raw_block: str, pass_n: int, base_text: str) -> tuple[str, str]:
                """Inject allow-line + prelude into a raw gen_tests block, reject
                blocks with no #[test] fns, then write it as sibling `pass_n` on
                top of `base_text` and run the compile-fix loop. Returns
                (status, final_block), status ∈ {"committed","no_tests","failed"}.
                On failure reverts the file to base_text — so in split-and-merge
                any subset blocks already merged before this one survive."""
                nonlocal _pass_had_work
                block = raw_block
                block = _inject_allow_before_gen_mod(block, _allow_line)
                block = _inject_gen_prelude_imports(block)
                if _count_test_attrs(block) == 0:
                    _log(f"  [{module_name}] {rel_path} — LLM block has 0 #[test] functions; "
                         f"skipping write (no productive content).")
                    _log_task0c_attempt(project_dir, rel_path, attempt, block, "", "no_tests_in_block")
                    return "no_tests", block
                _prev_sigs: frozenset = frozenset()
                _fix_attempt = 0
                _sib = _gen_tests_path(abs_path, pass_n)
                _decl = _build_gen_tests_decl(_sib.name, pass_n, _allow_line,
                                              cluster_cfg_attr=_cluster_cfg_attr(_current_cluster))
                _src_with_decl = base_text.rstrip() + _decl
                while True:
                    abs_path.write_text(_src_with_decl, encoding="utf-8")
                    _sib.write_text(_extract_gen_tests_body(block), encoding="utf-8")
                    check = subprocess.run(
                        ["cargo", "test", "--no-run", "-p", module_name],
                        cwd=project_dir, capture_output=True, text=True,
                        timeout=BUILD_TIMEOUT, env=_cargo_env(),
                    )
                    if check.returncode == 0:
                        _log_task0c_attempt(project_dir, rel_path, attempt + _fix_attempt, block, "", "success")
                        _log(f"  [{module_name}] Appended gen_tests_{pass_n} to {rel_path} "
                             f"({len(uncov_lines)} uncovered lines targeted)")
                        _pass_had_work = True
                        return "committed", block
                    compile_err = (check.stdout + check.stderr).strip()
                    cur_sigs = _error_signatures_from_output(compile_err)
                    if "unclosed delimiter" in compile_err or "mismatched closing delimiter" in compile_err:
                        _log(f"  [{module_name}] Unclosed delimiter — reverting {rel_path}.")
                        _revert_gen_tests_pass(abs_path, base_text, _sib)
                        return "failed", block
                    if _fix_attempt > 0:
                        resolved = _prev_sigs - cur_sigs
                        if cur_sigs == _prev_sigs:
                            _log(f"  [{module_name}] No error improvement in {rel_path} — reverting.")
                            _log_task0c_attempt(project_dir, rel_path, attempt + _fix_attempt, block, compile_err, "compile_error")
                            _revert_gen_tests_pass(abs_path, base_text, _sib)
                            return "failed", block
                        _log(f"  [{module_name}] {len(resolved)} error(s) resolved in {rel_path}, "
                             f"{len(cur_sigs)} remaining — continuing.")
                    _fix_attempt += 1
                    _prev_sigs = cur_sigs
                    _log(f"  [{module_name}] Compile error in {rel_path} "
                         f"(fix attempt {_fix_attempt}) — sending to LLM.")
                    _log_task0c_attempt(project_dir, rel_path, attempt + _fix_attempt, block, compile_err, "compile_error")
                    _fix_history = _get_task0c_history(project_dir, rel_path)
                    _fix_history_section = (f"\n{_fix_history}\n") if _fix_history.strip() else ""
                    _compile_err_capped = compile_err[-3_000:] if len(compile_err) > 3_000 else compile_err
                    _fix_extra = len(_compile_err_capped) + len(block) + len(_fix_history_section) + 4000
                    _fix_core = secs['cargo'] + secs['full_source'] + secs['fn_bodies'] + secs['symbols']
                    _fix_optional = secs['integ_tests'] + secs['siblings']
                    _FIX_CHAR_LIMIT = TASK0C_PROMPT_CHAR_CAP
                    _fix_ctx = _fix_core + (_fix_optional if len(_fix_core) + len(_fix_optional) + _fix_extra < _FIX_CHAR_LIMIT else "")
                    fix_prompt = f"""The Rust test block you generated for `{rel_path}` does not compile.
{_TASK0C_FIX_THINKING_DIRECTIVE}Fix ONLY the test block so it compiles. Return the corrected <FILE path="gen_tests_block"> block.
{_fix_history_section}
Compile error (read carefully — this is the ONLY thing to fix):
{_compile_err_capped}

Current test block (fix this):
{block}

--- Full context below — same constraints apply as when you generated the block ---
{_fix_ctx}
Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access module items.
- Read field types from the struct definitions in the source file — match EXACTLY.

INTERNAL MODULE RULES — these tests live inside the source file, not in an external test crate:
- Do NOT write `use <crate_name>::...` — you are already inside the crate.
- The template prelude already imports: {_prelude_names} — use them directly.
- Do NOT use `#[macro_export]` macros inside this module — they cannot be imported from within the crate.
  Construct values through direct API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
{_nightly_rules_for_cluster()}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}"""
                    fix_response = ask(fix_prompt, model=_TASK0C_MODEL, think=None)
                    fixed = extract_test_mod(fix_response)
                    if fixed:
                        block = _strip_trailing_file_tag(fixed)
                        block = _inject_allow_before_gen_mod(block, _allow_line)
                    else:
                        _log(f"  [{module_name}] LLM returned no fix — reverting {rel_path}.")
                        _revert_gen_tests_pass(abs_path, base_text, _sib)
                        return "failed", block

            block = ""
            _block_committed = False
            _did_split = False
            _sibling_path = _gen_tests_path(abs_path, _pass_n)
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
                    _base = _raw_text
                    _n_ok = 0
                    for _resp in _responses:
                        _raw_sub = extract_test_mod(_resp)
                        if _raw_sub:
                            _raw_sub = _strip_trailing_file_tag(_raw_sub)
                        if not _raw_sub:
                            continue
                        _pn = _next_gen_tests_n(abs_path)
                        _st, _ = _finalize_and_commit(_raw_sub, _pn, _base)
                        if _st == "committed":
                            _base = abs_path.read_text(encoding="utf-8")
                            _sibling_path = _gen_tests_path(abs_path, _pn)
                            _n_ok += 1
                    _block_committed = _n_ok > 0
                    _log(f"  [{module_name}] split-and-merge {rel_path}: merged "
                         f"{_n_ok}/{len(_partitions)} subset block(s)")
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
                        "RULES (mandatory):\n"
                        "- Do NOT write `use <crate_name>::...` — you are already inside the crate.\n"
                        "- Do NOT use `#[macro_export]` macros — unavailable inside the crate.\n"
                        + _nightly_rules_for_cluster() + "\n"
                        + "- Do NOT use `assert_eq!`/`assert_ne!` on types without `PartialEq` — use `format!` or `matches!()`.\n"
                        "- Each `#[test]` fn must have ZERO arguments.\n"
                        f"{_file_constraints_section}"
                    )
                    _query = (
                        f"You are writing Rust unit tests for `{rel_path}`.\n"
                        f"Target file: {rel_path}\n"
                        f"{_describe_test_environment()}"
                        f"{secs['cargo']}{secs['full_source']}{secs['fn_bodies']}{secs['symbols']}"
                        f"{_medium_rules}"
                        f"\nWrite a `#[cfg(test)] mod {mod_name}` block covering the uncovered lines shown above.\n"
                        f"{output_format}\n"
                    )
                # Use think=None — lets MODEL_CAPS decide (think=False for qwen3.6:27b-q8).
                # think=True on the compat path is unreliable: sometimes generates 40K chars
                # of thinking, sometimes generates raw_len=0. think=False (via /no_think)
                # is consistently reliable for this model.
                response = ask(_query, model=_TASK0C_MODEL, think=None)

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
                _status, block = _finalize_and_commit(block, _pass_n, _raw_text)
                if _status == "no_tests":
                    continue
                _block_committed = (_status == "committed")
                if _block_committed:
                    _sibling_path = _gen_tests_path(abs_path, _pass_n)
            elif not _did_split:
                _log_task0c_attempt(project_dir, rel_path, attempt, "", "", "no_block")
                _log(f"  [{module_name}] No test block extracted for {rel_path}.")

            # Quick per-file coverage snapshot after a successful append.
            _qcov_after: int | None = None
            if _block_committed:
                _qcov_result = _quick_file_coverage(project_dir, module_name, rel_path)
                if _qcov_result is not None:
                    _qcov, _qcov_able = _qcov_result
                    _qcov_after = _qcov
                    _qpct = (_qcov / _qcov_able * 100.0) if _qcov_able else 0.0
                    _log(f"  [{module_name}] {rel_path} quick-cov: {_qcov}/{_qcov_able} ({_qpct:.1f}%)")
                    # Persist result so the monitor can merge it into the files overview.
                    _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                    try:
                        _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                        _cache[rel_path] = {"covered": _qcov, "coverable": _qcov_able, "ts": time.time()}
                        _qcache.write_text(json.dumps(_cache))
                    except Exception:
                        pass
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
                    if _prev_qcov is not None and _qcov <= _prev_qcov:
                        file_failed_blocks.setdefault(rel_path, []).append(block)
                        _log(f"  [{module_name}] {rel_path} — block produced no coverage "
                             f"increase (was {_prev_qcov}, now {_qcov}); reverting.")
                        _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                        _block_committed = False
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
                    _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1}/{_INNER_RETRY_LIMIT} "
                         f"— {len(uncov_lines)} line(s) still uncovered.")
                    _mod_name_retry = f"gen_tests_retry{attempt}_{_ir}"
                    _retry_prompt = (
                        f"The test block you just wrote for `{rel_path}` compiled and ran "
                        f"but did NOT cover these specific lines: {sorted(uncov_lines)}\n\n"
                        f"{_guard_block}"
                        f"The function ran but took the OTHER branch. "
                        f"Write a NEW `#[cfg(test)] mod {_mod_name_retry}` block containing "
                        f"ONLY tests that exercise the specific branch conditions above. "
                        f"Do not repeat tests from the existing block.\n\n"
                        f"Source (uncovered lines marked):\n```rust\n"
                        f"{_annotate_uncovered(existing_text, uncov_lines)[:_SRC_SNIP_CAP]}\n```\n\n"
                        f"Existing test block (already on disk — do NOT duplicate):\n```rust\n"
                        f"{_old_block[:2000] if _old_block else '(none)'}\n```\n\n"
                        f"Output ONLY the new mod block wrapped in <FILE path=\"{rel_path}\">...</FILE>."
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
                    _retry_pass_n = _next_gen_tests_n(abs_path)
                    _retry_sibling = _gen_tests_path(abs_path, _retry_pass_n)
                    _retry_decl = _build_gen_tests_decl(_retry_sibling.name, _retry_pass_n, _allow_line,
                                                        cluster_cfg_attr=_cluster_cfg_attr(_current_cluster))
                    _retry_sibling.write_text(_extract_gen_tests_body(_retry_block), encoding="utf-8")
                    abs_path.write_text(_current_text.rstrip() + _retry_decl, encoding="utf-8")
                    _retry_check = subprocess.run(
                        ["cargo", "test", "--no-run", "-p", module_name],
                        cwd=project_dir, capture_output=True, text=True,
                        timeout=BUILD_TIMEOUT, env=_cargo_env(),
                    )
                    if _retry_check.returncode != 0:
                        _log(f"  [{module_name}] Quick-retry {_ir + 1}: compile failed — reverting.")
                        _revert_gen_tests_pass(abs_path, _current_text, _retry_sibling)
                        break
                    _log(f"  [{module_name}] Quick-retry {_ir + 1}: compiled — checking coverage.")
                    _qr_result = _quick_file_coverage(project_dir, module_name, rel_path)
                    if _qr_result is not None:
                        _qr_cov, _qr_able = _qr_result
                        _qr_pct = (_qr_cov / _qr_able * 100.0) if _qr_able else 0.0
                        _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1} cov: "
                             f"{_qr_cov}/{_qr_able} ({_qr_pct:.1f}%)")
                        _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                        try:
                            _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                            _cache[rel_path] = {"covered": _qr_cov, "coverable": _qr_able,
                                                "ts": time.time()}
                            _qcache.write_text(json.dumps(_cache))
                        except Exception:
                            pass
                        if _qr_cov > _qcov_after:
                            _log(f"  [{module_name}] Quick-retry {_ir + 1}: improvement "
                                 f"+{_qr_cov - _qcov_after} lines — stopping inner retries.")
                            _qcov_after = _qr_cov
                            _pass_had_work = True
                            break
                    else:
                        break

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
                if (stall_n >= 1) and fn_bodies.strip():
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
                        _pragma_fns = _retro_cats["UNREACHABLE"] + _retro_cats["CFG_GATED"]
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
                                    _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                                    try:
                                        _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                                        _cache[rel_path] = {"covered": _pp_cov, "coverable": _pp_able, "ts": time.time()}
                                        _qcache.write_text(json.dumps(_cache))
                                    except Exception:
                                        pass
                                    if _pp_cov >= _pp_able:
                                        _log(f"  [{module_name}] {rel_path} fully covered after pragma — "
                                             f"skipping further generation.")
                                        skipped_files.add(rel_path)
                                # Reset stall so the file gets another generation pass for
                                # any remaining TESTABLE/NEEDS_INTERNALS lines.
                                file_stall_counts[rel_path] = 0
                if stall_n >= FILE_STALL_LIMIT:
                    _log(f"  [{module_name}] {rel_path} stalled for {FILE_STALL_LIMIT} consecutive "
                         f"attempts with no improvement — skipping this file.")
                    skipped_files.add(rel_path)

            # Update snapshot for next attempt's line-coverage delta.
            file_prev_uncov_lines[rel_path] = set(uncov_lines)

        # If this entire pass had no LLM calls (all files filtered by cfg/artifact with
        # nothing actionable), there is nothing more the pipeline can do — stop immediately.
        if not _pass_had_work:
            _log(f"  [{module_name}] All files filtered (cfg/artifact) — no actionable uncovered lines. Stopping.")
            _at_ceiling = True
            break

        # If every remaining uncovered file is now skipped, die with a report.
        remaining = set(uncovered_by_file.keys()) - skipped_files
        if not remaining and skipped_files and coverage < _COVERAGE_TARGET:
            _log(f"\n  [{module_name}] HARD BLOCK: all uncovered files are stalled. "
                 f"Coverage: {coverage:.1f}%. Cannot reach {_COVERAGE_TARGET:.0f}%.")
            _log("  Stalled files and their uncovered line counts:")
            for sf in sorted(skipped_files):
                n_uncov = len(uncovered_by_file.get(sf, (None, set()))[1])
                history = _get_task0c_history(project_dir, sf)
                retro = file_retrospectives.get(sf, "")
                retro_note = f"\n      retrospective: {retro[:300]}" if retro else ""
                _log(f"    {sf}: {n_uncov} uncovered lines. Last attempts:\n{history}{retro_note}")
            _log("  Action required: improve the generation prompt or add manual tests for these files.")
            return coverage, covered_lines, False

        # --- Graceful stop check ---
        # If /tmp/pipeline_graceful_stop exists, finish this attempt cleanly and exit.
        _graceful_sentinel = Path("/tmp/pipeline_graceful_stop")
        if _graceful_sentinel.exists():
            with contextlib.suppress(OSError):
                _graceful_sentinel.unlink()
            _log(f"  [{module_name}] Graceful stop requested — attempt {attempt} complete. Exiting.")
            sys.exit(0)

    # M4 (FR-039): behaviour-hardening pass — mutation-kill as the strength
    # metric on top of line coverage. Opt-in via TASK0C_MUTATION_HARDEN; a
    # no-op (returns 0) otherwise, so the default flow is unchanged.
    try:
        _rel_files = [
            str(p.relative_to(project_dir))
            for p in sorted(module_dir.rglob("*.rs"))
            if not _is_gen_tests_file(p.name)
            and str(p.relative_to(project_dir)) not in existing_test_files
        ]
        _hard = _task0c_mutation_pass(project_dir, module_name, _current_cluster, _rel_files)
        if _hard:
            _log(f"  [{module_name}/{_cluster_label}] mutation-hardened {_hard} file(s).")
    except Exception as _me:
        _log(f"  [{module_name}/{_cluster_label}] mutation-harden pass skipped ({_me}).")

    _log(f"  [{module_name}/{_cluster_label}] Finished augmentation: {coverage:.1f}% coverage after {attempt} attempt(s).")
    return coverage, covered_lines, _at_ceiling
