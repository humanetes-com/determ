"""Phase-B test-grouping use-case (P6 relocation of `pipeline._task2_group_tests`).

Asks the model to partition a cluster's test files into cohesive reconstruction
units, validates/auto-recovers the JSON manifest, enforces the max-group-size
split, and persists `groups.json`. Falls back to one-group-per-file on any LLM or
validation failure. The model (`ask`), logger, and skeleton loader are injected;
the pure rules live in `domain.test_grouping`, the prompt in `prompts.test_grouping`.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.domain.test_grouping import (
    collect_skeleton_symbols,
    extract_groups_or_none,
    fallback_groups_per_file,
    parse_and_validate_groups,
    split_oversized_groups,
)
from pipeline_core.prompts.test_grouping import build_grouping_prompt, format_skeleton_for_prompt


def group_tests(
    skeleton_dir: Path,
    test_files: list[tuple[str, str]],
    cluster_name: str,
    module_name: str,
    *,
    ask: Callable[..., str],
    model: str | None,
    log: Callable[[str], None],
    load_skeleton_files: Callable[[Path], list[tuple[str, str]]],
) -> list[dict[str, Any]]:
    """Phase B: ask the LLM to group test files into cohesive units.

    Returns a list of group dicts: {name, rationale, test_files, target_symbols}.
    Falls back to one-group-per-test-file on LLM failure or validation failure.
    Result is also written to skeleton_dir/groups.json.
    """
    out_path = skeleton_dir / "groups.json"
    skeleton_files = load_skeleton_files(skeleton_dir)
    if not skeleton_files or not test_files:
        groups = fallback_groups_per_file(test_files)
        out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
        return groups

    skeleton_block = format_skeleton_for_prompt(skeleton_files)
    tests_block = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in test_files
    )
    sym_set = collect_skeleton_symbols(skeleton_files)
    test_paths = sorted({rel for rel, _ in test_files})

    prompt = build_grouping_prompt(module_name, cluster_name, skeleton_block, tests_block, test_paths)

    for attempt in range(2):
        response = ask(prompt, model=model)
        validated, err, recoverable_missing = parse_and_validate_groups(
            response, test_paths, sym_set
        )
        if validated is not None:
            # Post-process to enforce max group size = 3.
            validated = split_oversized_groups(validated, max_size=3)
            log(f"  [phase-b] LLM produced {len(validated)} group(s) (post-split).")
            out_path.write_text(json.dumps(validated, indent=2), encoding="utf-8")
            return validated
        # Auto-recover small omissions (≤2 missing test files) by appending
        # them to a "miscellaneous" group rather than burning another full LLM
        # round-trip. Compile-fail tests (trybuild) tend to get silently
        # dropped by the model.
        if recoverable_missing and len(recoverable_missing) <= 2 and attempt == 0:
            # Re-parse to get the partial groups (validator returned None when
            # it rejected, but the parsed structure is still useful).
            partial = extract_groups_or_none(response)
            if partial is not None:
                partial.append({
                    "name": "miscellaneous_auto_added",
                    "rationale": "auto-appended test files omitted by LLM grouping",
                    "test_files": sorted(recoverable_missing),
                    "target_symbols": [],
                })
                # Apply the same split even on the auto-recovered path so big
                # groups still get chunked.
                partial = split_oversized_groups(partial, max_size=3)
                log(f"  [phase-b] auto-recovered {len(recoverable_missing)} "
                    f"omitted test file(s) into a 'miscellaneous' group "
                    f"({len(partial)} groups post-split).")
                out_path.write_text(json.dumps(partial, indent=2), encoding="utf-8")
                return partial
        log(f"  [phase-b] attempt {attempt + 1} rejected: {err}")

    log("  [phase-b] LLM grouping failed validation twice — falling back to per-file groups.")
    groups = fallback_groups_per_file(test_files)
    out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
    return groups
