"""Phase-B test-grouping domain logic (P6 relocation).

Pure rules for grouping a cluster's test files into cohesive reconstruction
units and for parsing/validating the LLM's JSON grouping manifest. Stdlib +
domain scanners only (no I/O, no LLM). The use-case
``application.reconstruct.test_grouping.group_tests`` orchestrates these over an
injected model; the prompt assembly lives in ``prompts.test_grouping``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.domain.rust_idents import fn_decl_names
from pipeline_core.domain.rust_scan import line_has_assertion
from pipeline_core.domain.text import strip_simple_code_fence


def collect_skeleton_symbols(skeleton_files: list[tuple[str, str]]) -> set[str]:
    """Return the set of fn/method names declared in the skeleton.

    Used by Phase B validation: every target_symbol the LLM emits should exist
    in this set.
    """
    syms: set[str] = set()
    for _, content in skeleton_files:
        syms |= fn_decl_names(content)
    return syms


def split_oversized_groups(groups: list[dict[str, Any]], max_size: int = 3) -> list[dict[str, Any]]:
    """Split any group with >max_size test files into smaller chunks.

    Each chunk inherits the parent's rationale + target_symbols, with the
    name suffixed by a 1-based index. Keeps target_symbols on every chunk
    so leaf-first ordering still works.
    """
    out: list[dict[str, Any]] = []
    for g in groups:
        tests = list(g.get("test_files", []))
        if len(tests) <= max_size:
            out.append(g)
            continue
        base_name = g.get("name", "group")
        chunks: list[list[str]] = [tests[i:i + max_size] for i in range(0, len(tests), max_size)]
        for idx, chunk in enumerate(chunks, 1):
            out.append({
                "name": f"{base_name}_{idx}of{len(chunks)}",
                "rationale": f"{g.get('rationale', '')} (auto-split {idx}/{len(chunks)})",
                "test_files": chunk,
                "target_symbols": list(g.get("target_symbols", [])),
            })
    return out


def extract_groups_or_none(response: str) -> list[dict[str, Any]] | None:
    """Parse the LLM's JSON manifest into a groups list without validation."""
    txt = strip_simple_code_fence(response.strip())
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "groups" not in data:
        return None
    g = data["groups"]
    return g if isinstance(g, list) else None


def parse_and_validate_groups(
    response: str, test_paths: list[str], known_symbols: set[str]
) -> tuple[list[dict[str, Any]] | None, str, set[str]]:
    """Parse the LLM's JSON manifest, validate coverage and symbol existence.

    Returns (groups, error_message, recoverable_missing).
      - groups is None on validation failure.
      - recoverable_missing is the set of test files the LLM omitted; populated
        only when the failure was a coverage gap (not a structural error).
    """
    # Tolerate fenced JSON.
    txt = strip_simple_code_fence(response.strip())
    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        return None, f"not valid JSON: {e}", set()
    if not isinstance(data, dict) or "groups" not in data:
        return None, "missing 'groups' key", set()
    groups = data["groups"]
    if not isinstance(groups, list) or not groups:
        return None, "'groups' must be a non-empty list", set()

    seen_tests: set[str] = set()
    expected_tests = set(test_paths)
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            return None, f"group #{i} is not an object", set()
        for key in ("name", "rationale", "test_files", "target_symbols"):
            if key not in g:
                return None, f"group #{i} missing key '{key}'", set()
        if not isinstance(g["test_files"], list) or not g["test_files"]:
            return None, f"group '{g.get('name','?')}' has empty test_files", set()
        for tf in g["test_files"]:
            if tf in seen_tests:
                return None, f"test file '{tf}' appears in multiple groups", set()
            seen_tests.add(tf)
        # Symbol existence is a soft check — warn but don't reject.
        # (LLM may use slight name variations like `Self::method`.)

    missing = expected_tests - seen_tests
    extra = seen_tests - expected_tests
    if missing:
        return None, f"groups omit {len(missing)} test file(s); first: {sorted(missing)[:3]}", missing
    if extra:
        return None, f"groups include unknown test file(s): {sorted(extra)[:3]}", set()
    return groups, "ok", set()


def fallback_groups_per_file(test_files: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """One group per test file — deterministic safe default."""
    groups: list[dict[str, Any]] = []
    for rel, _ in test_files:
        stem = Path(rel).stem
        groups.append({
            "name": f"file_{stem}",
            "rationale": f"single-file fallback grouping for {rel}",
            "test_files": [rel],
            "target_symbols": [],
        })
    return groups


def extract_test_assertions(test_files: list[tuple[str, str]], module_dir_rel: str = "",
                            max_assertions: int = 40) -> str:
    """Extract assert_* lines with minimal surrounding context from test files.
    Returns a compact 'assertion manifest' TASK 2 can use as ground truth. Only
    includes files relevant to the current module (if module_dir_rel given)."""
    results: list[str] = []
    for rel, content in test_files:
        if module_dir_rel and not (rel.startswith(module_dir_rel) or
                                   f"/{module_dir_rel}" in rel):
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if line_has_assertion(stripped) and not stripped.startswith('//'):
                context_line = lines[i - 1].strip() if i > 0 else ""
                entry = (f"  {context_line}  // context\n  {stripped}"
                         if context_line and context_line.startswith("let")
                         else f"  {stripped}")
                results.append(entry)
                if len(results) >= max_assertions:
                    break
        if len(results) >= max_assertions:
            break
    if not results:
        return ""
    return ("Assertions the generated code must satisfy (from test files):\n"
            + "\n".join(results))
