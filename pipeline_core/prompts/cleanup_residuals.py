"""Phase D residual-cleanup prompt (pure assembly).

Builds the single-shot prompt that asks the model to fill the specific
`unimplemented!()` bodies the tests still reach, given the current on-disk
skeleton and the relevant test files. No I/O, no LLM call.
"""
from __future__ import annotations

from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.text import smart_truncate


def build_cleanup_residuals_prompt(
    *,
    module_name: str,
    by_file: dict[str, list[str]],
    skeleton_block: str,
    test_files: list[tuple[str, str]],
    functional_req: str,
    single_call_char_limit: int,
    file_format_instruction: str,
) -> str:
    """Assemble the Phase D fill prompt. `by_file` maps each affected file to
    the still-stubbed fn names the tests call; only test files mentioning any
    target fn are surfaced (relevance filter)."""
    targets_block = "\n".join(
        f"  - {rel}: {', '.join(fns)}" for rel, fns in by_file.items()
    )
    relevant_tests: list[str] = []
    for rel, content in test_files:
        if any(contains_word(content, fn)
               for fns in by_file.values() for fn in fns):
            relevant_tests.append(f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>')
    tests_block = "\n\n".join(relevant_tests)
    fr_cap = single_call_char_limit // 4
    fr_capped = smart_truncate(functional_req or "", total_limit=fr_cap,
                               head=fr_cap // 3, tail=2 * fr_cap // 3)

    return f"""You are cleaning up residual `unimplemented!()` bodies in a Rust crate after a
phase of group-driven test-driven fills.

Crate: `{module_name}`

These specific functions are still `unimplemented!()` but the tests call them:
{targets_block}

Current source on disk:
{skeleton_block}

Tests that reference these functions:
{tests_block}

Functional requirements:
{fr_capped}

{file_format_instruction}

Output the source files you modified. Fill ONLY the functions listed above.
Do NOT modify any function whose body is NOT `unimplemented!()`.
Do NOT modify type definitions, imports, or any other items.
"""
