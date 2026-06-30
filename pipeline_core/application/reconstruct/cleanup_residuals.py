"""Phase D — residual `unimplemented!()` cleanup use-case.

After the group-driven fills, some stubs may remain. This pass classifies the
remainder as test-exercised vs not, issues ONE LLM call to fill the exercised
ones (the others never run, so cargo test won't trigger them), merges the
result onto disk preserving existing bodies, and reports the tally.

I/O + the LLM call are injected via `CleanupResidualsDeps` (ISP roles); the
prompt is assembled by `prompts.cleanup_residuals` (pure).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.fill_merge import (
    merge_fills_preserving_existing,
)
from pipeline_core.domain.llm_output import extract_files
from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.prompts.cleanup_residuals import build_cleanup_residuals_prompt
from pipeline_core.prompts.test_grouping import format_skeleton_for_prompt


@dataclass(frozen=True)
class CleanupResidualsDeps:
    scan_unimplemented: Callable[[Path], list[tuple[str, str]]]
    load_skeleton_files: Callable[[Path], list[tuple[str, str]]]
    ask: Callable[..., str]
    task2_model: str
    single_call_char_limit: int
    file_format_instruction: str
    log: Callable[[str], None]


def filter_test_exercised(
    stubs: list[tuple[str, str]], test_files: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep only stubs whose function name appears (as a token) in any test file."""
    if not stubs or not test_files:
        return []
    test_blob = "\n".join(content for _, content in test_files)
    return [(rel, name) for rel, name in stubs if contains_word(test_blob, name)]


def cleanup_residuals(
    target_src_dir: Path,
    project_dir: Path,
    functional_req: str,
    test_files: list[tuple[str, str]],
    module_name: str,
    *,
    deps: CleanupResidualsDeps,
) -> dict[str, int]:
    """Scan for residual stubs and patch what the tests need.

    Returns {'stubs_total', 'stubs_exercised', 'files_updated', 'stubs_remaining'}.
    """
    stubs = deps.scan_unimplemented(target_src_dir)
    if not stubs:
        return {"stubs_total": 0, "stubs_exercised": 0,
                "files_updated": 0, "stubs_remaining": 0}

    exercised = filter_test_exercised(stubs, test_files)
    deps.log(f"  [phase-d] {len(stubs)} residual stub(s); "
             f"{len(exercised)} reachable from tests.")

    if not exercised:
        return {"stubs_total": len(stubs), "stubs_exercised": 0,
                "files_updated": 0, "stubs_remaining": len(stubs)}

    # Group exercised stubs by file so the model sees each affected file as one unit.
    by_file: dict[str, list[str]] = {}
    for rel, fn in exercised:
        by_file.setdefault(rel, []).append(fn)

    skeleton_block = format_skeleton_for_prompt(
        deps.load_skeleton_files(target_src_dir))
    prompt = build_cleanup_residuals_prompt(
        module_name=module_name,
        by_file=by_file,
        skeleton_block=skeleton_block,
        test_files=test_files,
        functional_req=functional_req,
        single_call_char_limit=deps.single_call_char_limit,
        file_format_instruction=deps.file_format_instruction,
    )
    response = deps.ask(prompt, model=deps.task2_model)
    files = extract_files(response)
    files_updated = 0
    for rel, model_text in files.items():
        if ".gen_tests_" in rel:
            continue  # never touch task0c protected test artifacts
        disk_path = target_src_dir / rel
        if not disk_path.exists():
            continue
        disk_text = disk_path.read_text(encoding="utf-8")
        merged = merge_fills_preserving_existing(disk_text, model_text)
        if merged != disk_text:
            disk_path.write_text(merged, encoding="utf-8")
            files_updated += 1

    remaining = deps.scan_unimplemented(target_src_dir)
    return {
        "stubs_total": len(stubs),
        "stubs_exercised": len(exercised),
        "files_updated": files_updated,
        "stubs_remaining": len(remaining),
    }
