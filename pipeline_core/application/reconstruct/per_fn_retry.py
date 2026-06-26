"""Per-fn retry use-case — extracted from _task3_inline_retry (V1 / §7.0b).

`fix_one_ref` re-asks the model for one (file, fn) body and splices it in.
`apply_per_fn_fixes` drives B2 isolation: one fn's exception is logged and
skipped, never aborting the improvement pass or masking the other fns.

Extracted from the closure nest so B2's fault-isolation behaviour is
harness-testable without monkeypatching module globals.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.prompts.context import PerFnFixSections


@dataclass(frozen=True, slots=True)
class RetryContext:
    """Per-run data previously closed over by _task3_inline_retry locals."""
    project_dir: Path
    src_root: Path
    cluster_label: str
    containers: dict[tuple[str, str], str]
    type_recs: list[Any]
    fr_path: Path | None
    tests_dir_files: list[Path]


@dataclass(frozen=True, slots=True)
class RetryDeps:
    """Collaborators injected by the composition root (pipeline.py)."""
    locate_fn: Callable[[str, str], tuple[Any, ...] | None]
    gather_fix_context: Callable[..., Any]
    build_per_fn_fix_prompt: Callable[..., str]
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    splice_fn_body: Callable[..., tuple[bool, int]]
    find_tests_for_fn: Callable[..., list[Any]]
    focus_cargo_errors: Callable[[str, str], str]
    is_gen_tests_file: Callable[[str], bool]
    log: Callable[[str], None]


def fix_one_ref(
    rel_f: str, fn: str, output: str, ctx: RetryContext, deps: RetryDeps,
) -> bool:
    """Re-ask the model for one (file, fn) body and splice it; True if spliced.

    Frozen tests/gen_tests files: return False (test oracle is immutable).
    Benign no-ops (missing file, fn not located, empty model body): False.
    Transport/model/splice failures: RAISE — the caller isolates per-fn (B2).
    """
    if rel_f.startswith("tests/") or deps.is_gen_tests_file(Path(rel_f).name):
        return False
    fp = ctx.project_dir / rel_f
    if not fp.exists():
        return False
    try:
        cur_text = fp.read_text(encoding="utf-8")
    except Exception:
        return False
    sloc = deps.locate_fn(cur_text, fn)
    if sloc is None:
        return False
    sig_t = sloc[0]
    cur_body = cur_text[sloc[2]:sloc[3]]
    err_excerpt = deps.focus_cargo_errors(output, rel_f)
    _src_rel = rel_f[4:] if rel_f.startswith("src/") else rel_f
    fix_ctx = deps.gather_fix_context(
        file_text=cur_text, file_rel=rel_f, fn_name=fn,
        sig_text=sig_t, body_text=cur_body,
        error_excerpt=err_excerpt,
        container=ctx.containers.get((_src_rel, fn), ""),
        type_records=ctx.type_recs,
        fr_path=ctx.fr_path,
    )
    t_blocks = deps.find_tests_for_fn(
        ctx.tests_dir_files, fn, file_rel=rel_f,
        src_dir=ctx.src_root, container=fix_ctx.container,
    )
    prompt = deps.build_per_fn_fix_prompt(
        file_text=cur_text, fn_name=fn, sig_text=sig_t,
        test_blocks=t_blocks, module_name=ctx.project_dir.name,
        cluster_name=ctx.cluster_label, file_rel=rel_f,
        prev_body=cur_body, error_excerpt=err_excerpt,
        sections=PerFnFixSections(
            container=fix_ctx.container, types=fix_ctx.types_section,
            notes=fix_ctx.notes_section,
            macros="", invariants="", retrospective_guidance="",
            siblings="", crate_idioms="",
            bound_methods=fix_ctx.bound_methods_section,
        ),
        fr_path=ctx.fr_path,
    )
    new_body, _ = deps.ask_per_fn_body(prompt, fn, [], sig_text=sig_t)
    if not new_body:
        return False
    ok, _ = deps.splice_fn_body(fp, fn, new_body, allow_nonstub=True)
    if ok:
        deps.log(f"  [task3-retry] re-spliced {rel_f}::{fn}.")
        return True
    return False


def apply_per_fn_fixes(
    refs: list[tuple[str, str]], output: str, ctx: RetryContext, deps: RetryDeps,
) -> int:
    """Re-ask + splice each (file, fn). B2: one fn's exception is logged and
    skipped — never aborts the pass, never masks the other fixable frontier fns.
    """
    fixed_count = 0
    for rel_f, fn in refs:
        try:
            if fix_one_ref(rel_f, fn, output, ctx, deps):
                fixed_count += 1
        except Exception as e:
            deps.log(
                f"  [task3-retry] fn {rel_f}::{fn} skipped: {type(e).__name__}: {e}"
            )
    return fixed_count
