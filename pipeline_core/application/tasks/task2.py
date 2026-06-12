"""TASK 2 — source generation dispatcher + inner build-fix loop + post-test fix loop.

Extracted from pipeline.py (slice 5b, Option 1). The generation engine (the Task2Generator
classes + `generate_fix_loop`) stays in pipeline.py and is injected. The two inline regexes
in the inner loop (Cargo.toml proc-macro flag, rustc error-code count) are kept in pipeline.py
behind injected predicates so the clean layer stays regex-free. No import of pipeline.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.text import trim_to_line
from ...log import emit, log


@dataclass(frozen=True, slots=True)
class Task2Deps:
    """Collaborators TASK 2 needs, injected by the composition root."""

    generator_factory: Callable[[], Any]
    load_manifest: Callable[[Any], list[str]]
    read: Callable[[Any], str]
    generate_fix_loop: Callable[..., tuple[Any, ...]]
    store_prompt_content: Callable[[str], Any]
    is_proc_macro_crate: Callable[[str], bool]     # keeps the Cargo.toml regex
    count_error_codes: Callable[[str], int]        # keeps the rustc-error regex
    build_check_retries: int
    build_max_stalls: int
    build_timeout: int
    test_timeout: int
    task2_model: str
    task2_fix_retries: int
    single_call_char_limit: int
    genesis_cap: int
    # Builds the per-cluster strict check for the post-task3 fix loop:
    # factory(project_dir) → check_fn running EVERY baked cluster, or None
    # when the workspace has no baked clusters (legacy single-command path).
    cluster_check_factory: Callable[[Path], Callable[[], tuple[bool, str]] | None] | None = None


def task2(project_dir: Path, genesis_path: Path, functional_req_path: Path,
          manifest_path: Path, existing_test_files: Any,
          deps: Task2Deps,
          project_profile: str = "", workspace_structure: str = "", build_error: str = "",
          locked_dep_context: str = "", current_module: str = "", module_dir_rel: str = "",
          whole_project_context: str = "", coverage_strategy: str = "", api_surface: str = "",
          prev_source_context: str = "") -> Any:
    """
    Generate source files from functional requirements.
    Implementation is selected by TASK2_GENERATOR in .env (default: static).
    """
    return deps.generator_factory().generate(
        project_dir, genesis_path, functional_req_path, manifest_path,
        existing_test_files, project_profile, workspace_structure, build_error,
        locked_dep_context, current_module, module_dir_rel,
        whole_project_context=whole_project_context,
        coverage_strategy=coverage_strategy,
        api_surface=api_surface,
        prev_source_context=prev_source_context,
    )


def task2_inner_loop(project_dir: Path, genesis_path: Path, functional_req_path: Path,
                     manifest_path: Path, existing_test_files: Any,
                     project_profile: str, workspace_structure: str,
                     build_check_cmd: str | None, deps: Task2Deps, locked_dep_context: str = "",
                     current_module: str = "", module_dir_rel: str = "",
                     whole_project_context: str = "", coverage_strategy: str = "",
                     api_surface: str = "", prev_source_context: str = "") -> bool:
    """
    Inner loop: TASK 2 (code generation) followed by a compile-only check.
    If the build check fails, feeds the compiler error back into TASK 2 and retries.
    Avoids burning full outer-loop iterations on structural code errors.

    Returns True if code was generated and (if a build check exists) compiles cleanly.
    Returns False if all retries are exhausted.
    """
    # Pre-detect proc-macro so fix-loop can pass the constraint as extra_context.
    _fix_extra_context = ""
    if module_dir_rel:
        _cargo_toml_check = project_dir / module_dir_rel / "Cargo.toml"
        if _cargo_toml_check.exists():
            _ct = _cargo_toml_check.read_text(encoding="utf-8")
            if deps.is_proc_macro_crate(_ct):
                _fix_extra_context = (
                    "\n⚠️  PROC-MACRO CRATE: `#[proc_macro_derive]`, `#[proc_macro]`, and "
                    "`#[proc_macro_attribute]` functions MUST be at the TOP LEVEL of lib.rs — "
                    "NEVER inside any `mod` block. Do NOT wrap the file in `mod lib { ... }`.\n"
                )

    build_error = ""
    _t2_t0 = time.monotonic()
    emit("task.start", task="task2", module=current_module,
         data={"max_attempts": deps.build_check_retries,
               "has_build_check": bool(build_check_cmd)})
    # P7.5: floor-stall against rustc error count across regen attempts.
    _t2_max_stalls = deps.build_max_stalls
    _t2_best_count: int | None = None
    _t2_stall = 0
    for attempt_num in range(1, deps.build_check_retries + 1):
        if attempt_num > 1:
            log(f"  Inner loop attempt {attempt_num}/{deps.build_check_retries} "
                f"(fixing build error from previous attempt)...")

        ok = task2(project_dir, genesis_path, functional_req_path, manifest_path,
                   existing_test_files, deps, project_profile, workspace_structure, build_error,
                   locked_dep_context, current_module, module_dir_rel,
                   whole_project_context=whole_project_context,
                   coverage_strategy=coverage_strategy,
                   api_surface=api_surface,
                   prev_source_context=prev_source_context if attempt_num == 1 else "")
        if not ok:
            # When module scope filtering discards all planned files the model listed
            # files for the wrong crates. Inject an explicit corrective error and retry
            # rather than aborting — the model needs one more chance with clear guidance.
            if module_dir_rel and attempt_num < deps.build_check_retries:
                scope_msg = (
                    f"CRITICAL SCOPE ERROR: Every file path you output was WRONG and was discarded.\n"
                    f"You MUST ONLY generate files for the `{current_module}` crate inside this Cargo workspace.\n"
                    f"Every file path MUST start with `{module_dir_rel}` (note: this includes the `src/` subdirectory).\n"
                    f"CORRECT examples:  {module_dir_rel}src/lib.rs  |  {module_dir_rel}src/parse_attrs.rs\n"
                    f"WRONG examples:    src/lib.rs  |  src/{current_module}/lib.rs  |  {current_module}/lib.rs\n"
                    f"Output ONLY files whose path starts with `{module_dir_rel}`."
                )
                log("  TASK 2 produced no in-scope files — injecting scope correction for retry...")
                build_error = scope_msg
                continue
            log("  TASK 2 failed to generate files — aborting inner loop.")
            emit("task.done", task="task2", module=current_module,
                 duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                 attempt=attempt_num,
                 data={"ok": False, "reason": "no_files_generated"})
            return False

        if not build_check_cmd:
            emit("task.done", task="task2", module=current_module,
                 duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                 attempt=attempt_num,
                 data={"ok": True, "build_checked": False})
            return True  # No build check available; proceed to full tests.

        # Use generate_fix_loop: run build check, if it fails ask LLM to fix only
        # the broken files rather than regenerating everything on the next attempt.
        # Keeps files that compiled correctly, targets only the broken ones.
        generated_paths = [
            project_dir / rel for rel in deps.load_manifest(manifest_path)
            if rel not in existing_test_files and (project_dir / rel).exists()
        ]
        try:
            _fix_loop_fr = deps.read(functional_req_path) if functional_req_path.exists() else ""
        except Exception:
            _fix_loop_fr = ""
        build_ok, build_output = deps.generate_fix_loop(
            project_dir=project_dir,
            check_cmd=build_check_cmd,
            source_paths=generated_paths,
            existing_test_files=existing_test_files,
            extra_context=_fix_extra_context,
            model=deps.task2_model,
            max_fix_attempts=4,
            timeout=deps.build_timeout,
            allowed_dir_prefix=module_dir_rel,
            functional_req=_fix_loop_fr,
            fr_path=functional_req_path,
        )
        if build_ok:
            log("  Build check PASSED.")
            emit("compile.success", module=current_module, attempt=attempt_num)
            emit("task.done", task="task2", module=current_module,
                 duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                 attempt=attempt_num,
                 data={"ok": True, "build_checked": True})
            return True

        log(f"  Build still failing after fix attempts (inner loop attempt {attempt_num}).")
        emit("compile.fail", module=current_module, attempt=attempt_num,
             data={"output": deps.store_prompt_content(build_output or "")})
        # P7.5: floor-stall against error count across regens.
        _t2_count = deps.count_error_codes(build_output or "")
        if _t2_best_count is None or _t2_count < _t2_best_count:
            _t2_best_count = _t2_count
            _t2_stall = 0
        else:
            _t2_stall += 1
        emit("loop.iter", data={
            "name": "task2.build_retries",
            "iter": attempt_num,
            "metric_new": _t2_count,
            "metric_best": _t2_best_count,
            "improved": _t2_stall == 0,
            "stalled": _t2_stall,
        })
        if _t2_stall >= _t2_max_stalls:
            log(f"  Inner loop floor stalled at {_t2_best_count} errors "
                f"for {_t2_stall} regen(s) — escalating to outer loop early.")
            break
        # Pass the error into the next full task2 regeneration so the model
        # understands what structural issues need to be addressed.
        build_error = build_output

    log(f"  Inner loop exhausted after {deps.build_check_retries} attempts — escalating to outer loop.")
    emit("task.done", task="task2", module=current_module,
         duration_ms=int((time.monotonic() - _t2_t0) * 1000),
         data={"ok": False, "reason": "retries_exhausted"})
    return False


def task2_fix_loop(
    project_dir: Path,
    manifest_path: Path,
    existing_test_files: set[str],
    test_output: str,
    test_command: str,
    functional_req_path: Path,
    deps: Task2Deps,
    genesis_path: Path | None = None,
    module_dir_rel: str = "",
) -> tuple[bool, str]:
    """
    After TASK 3 failure: targeted fix before full TASK 4 + regeneration.
    Thin wrapper around generate_fix_loop using the manifest's source files.
    """
    current_files = deps.load_manifest(manifest_path)
    if not current_files:
        return False, test_output

    source_paths = [
        project_dir / rel for rel in current_files
        if rel not in existing_test_files and (project_dir / rel).exists()
    ]

    functional_req = deps.read(functional_req_path)

    # Fill-down for extra_context: genesis → test files (stem-prioritized).
    _extra_budget = max(10_000, deps.single_call_char_limit - 10_000 - 30_000 - 8_000)

    # 1. Genesis — methodology, always first.
    _genesis_section = ""
    if genesis_path and genesis_path.exists():
        _genesis = deps.read(genesis_path)
        if _genesis.strip():
            _genesis_section = (
                f"\n--- genesis.md (methodology) ---\n"
                f"{trim_to_line(_genesis, deps.genesis_cap, chr(10) + '... (genesis truncated)')}\n"
            )

    # 2. Test files — stem-prioritized, fill remaining budget.
    _test_budget = max(0, _extra_budget - len(_genesis_section))
    _file_stem = Path(module_dir_rel.rstrip("/")).name if module_dir_rel else ""
    _primary_tests: list[str] = []
    _secondary_tests: list[str] = []
    for _rel in sorted(existing_test_files):
        if module_dir_rel and not _rel.startswith(module_dir_rel):
            continue
        _tp = project_dir / _rel
        if not _tp.exists():
            continue
        _blk = f'\n<TESTFILE path="{_rel}">\n{_tp.read_text(encoding="utf-8")}\n</TESTFILE>'
        if _file_stem and Path(_rel).name.split(".")[0] == _file_stem:
            _primary_tests.append(_blk)
        else:
            _secondary_tests.append(_blk)
    _test_parts = ["\nTest files (the tests your fix must pass):"]
    _test_used = 0
    for _blk in _primary_tests + _secondary_tests:
        if _test_used + len(_blk) > _test_budget:
            break
        _test_parts.append(_blk)
        _test_used += len(_blk)
    _test_section = ("\n".join(_test_parts) + "\n") if len(_test_parts) > 1 else ""

    extra = _genesis_section + _test_section

    # Verdict authority: the per-cluster strict runner, when baked clusters
    # exist. The bare test_command sees only the default cluster — validating
    # with it alone is the false-SUCCESS bug (2026-06-12 microcrate).
    check_fn = (deps.cluster_check_factory(project_dir)
                if deps.cluster_check_factory else None)

    return deps.generate_fix_loop(
        project_dir=project_dir,
        check_cmd=test_command,
        check_fn=check_fn,
        source_paths=source_paths,
        existing_test_files=existing_test_files,
        extra_context=extra,
        model=deps.task2_model,
        max_fix_attempts=deps.task2_fix_retries,
        timeout=deps.test_timeout,
        functional_req=functional_req,
        fr_path=functional_req_path,
    )
