"""Anti-corruption validators for TASK 1, 2, and 4 outputs.

Extracted from pipeline.py (H/P6). All checks are deterministic — no LLM calls.
Task2 validators receive impure deps (extractor calls) via `Task2CheckerDeps`.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.rust_idents import (
    _META_PHRASES,
    camelcase_type_names,
    defines_type,
    extract_identifiers,
)
from pipeline_core.domain.rust_scan import strip_comment_spans
from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES

# Justified: detecting model-output placeholder patterns in generated files
# (not Rust-source structure parsing — R7.5 category).
_PLACEHOLDER_RE = re.compile(
    r'unimplemented!\s*\(\)|todo!\s*\(\)|#\s*TODO|#\s*FIXME|pass\s*$|raise\s+NotImplementedError',
    re.MULTILINE,
)

# Justified: parsing the api_surface *listing* artifact (a summary text, NOT
# valid Rust source — syn cannot parse it; the generated-source side uses the
# extractor). See R7.2 in CODE_ARCHITECTURE.md.
_PUB_ITEM_RE = re.compile(
    r'\bpub(?:\([^)]*\))?\s+'
    r'(?:unsafe\s+)?(?:async\s+)?(?:const\s+)?(?:default\s+)?'
    r'(fn|struct|enum|trait|type|mod)\s+(\w+)',
)


@dataclass(frozen=True)
class Task2CheckerDeps:
    enumerate_types: Callable[[Path], list[TypeRecord] | None]
    enumerate_fns: Callable[[Path], list[dict[str, object]] | None]
    load_api_surface: Callable[[Path, str], str]


def validate_task1_output(req_path: Path, test_files: list[tuple[str, str]]) -> tuple[bool, str]:
    """Anti-corruption check before TASK 2. Returns (ok, reason)."""
    content = req_path.read_text(encoding="utf-8") if req_path.exists() else ""

    if len(content) < 500:
        return False, f"too short ({len(content)} chars, need ≥ 500)"

    content_lower = content.lower()
    for phrase in _META_PHRASES:
        if phrase in content_lower:
            return False, f"meta-commentary detected ('{phrase}')"

    req_idents = extract_identifiers(content)
    test_idents: set[str] = set()
    for _, tc in test_files:
        test_idents.update(extract_identifiers(tc))

    overlap = req_idents & test_idents
    if len(overlap) < 3:
        sample = sorted(overlap)[:5]
        return False, (
            f"requirements too generic — only {len(overlap)} identifiers overlap "
            f"with test files (need ≥ 3). Overlap: {sample}"
        )

    return True, "ok"


def validate_task4_output(
    genesis_before: str,
    genesis_after: str,
    test_output: str,
    failure_type: str = "test",
) -> tuple[bool, str]:
    """Anti-corruption check on genesis.md update. Returns (ok, reason)."""
    if genesis_after.strip() == genesis_before.strip():
        return False, "genesis_stall — TASK 4 produced no update to genesis.md"

    if len(genesis_after) <= len(genesis_before):
        return False, "genesis shrank — TASK 4 may have removed content"

    new_text = genesis_after[len(genesis_before):]

    if failure_type != "build":
        test_idents = extract_identifiers(test_output)
        new_idents = extract_identifiers(new_text)
        if test_idents and not (test_idents & new_idents):
            return False, "genesis update contains no identifiers from the failing test output"

    new_paragraphs = [p.strip() for p in new_text.split("\n\n") if len(p.strip()) > 100]
    for para in new_paragraphs:
        if para in genesis_before:
            return False, "genesis update duplicates existing content (paragraph already present)"

    return True, "ok"


def _extract_pub_items(text: str) -> set[tuple[str, str]]:
    """Return {(kind, name)} for every pub item in the api_surface listing artifact."""
    return {(m.group(1), m.group(2)) for m in _PUB_ITEM_RE.finditer(text)}


def _pub_items_from_tree(
    walk_root: Path,
    deps: Task2CheckerDeps,
) -> set[tuple[str, str]]:
    """Return {(kind, name)} for pub items in a Rust source tree, via the extractor."""
    def _excluded(rel: str) -> bool:
        parts = set(Path(rel).parts)
        return bool(parts & {"examples", "benches", "tests"}) or bool(parts & SKIP_DIR_NAMES)

    out: set[tuple[str, str]] = set()
    for t in deps.enumerate_types(walk_root) or []:
        if t.is_pub and t.kind in {"struct", "enum", "trait"} and not _excluded(t.file):
            out.add((t.kind, t.name))
    for f in deps.enumerate_fns(walk_root) or []:
        fn_file = str(f.get("file", ""))
        fn_name = str(f.get("fn_name", ""))
        if f.get("is_pub") and not _excluded(fn_file):
            out.add(("fn", fn_name))
    return out


def validate_task2_output(
    project_dir: Path,
    expected_paths: list[str],
    req_path: Path | None = None,
    deps: Task2CheckerDeps | None = None,
) -> tuple[bool, str]:
    """Anti-corruption check before TASK 3. Returns (ok, reason)."""
    if not expected_paths:
        return True, "ok (no expected paths recorded)"

    missing = [p for p in expected_paths if not (project_dir / p).exists()]
    if missing:
        return False, f"missing files: {', '.join(missing[:5])}"

    tiny = [
        p for p in expected_paths
        if (project_dir / p).exists() and (project_dir / p).stat().st_size < 50
    ]
    if tiny:
        return False, f"empty/stub files (< 50 bytes): {', '.join(tiny[:5])}"

    placeholder_files = []
    all_source = ""
    for p in expected_paths:
        fp = project_dir / p
        if not fp.exists():
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        all_source += raw
        stripped = strip_comment_spans(raw)
        stripped = "".join(stripped.split())
        placeholders_found = len(_PLACEHOLDER_RE.findall(raw))
        non_placeholder_len = len("".join(_PLACEHOLDER_RE.sub("", stripped).split()))
        if placeholders_found > 0 and non_placeholder_len < 50:
            placeholder_files.append(p)
    if placeholder_files:
        return False, (
            f"placeholder-only files (all unimplemented!/todo!): "
            f"{', '.join(placeholder_files[:3])}"
        )

    if req_path and req_path.exists():
        req_content = req_path.read_text(encoding="utf-8", errors="ignore")
        named_types = {w for w in camelcase_type_names(req_content) if len(w) >= 3}
        named_types -= {"README", "TODO", "FIXME", "API", "URL", "CLI", "LLM", "AI"}
        if named_types and deps is not None:
            _type_recs = deps.enumerate_types(project_dir)
            if _type_recs is not None:
                _defined = {r.name for r in _type_recs
                            if r.kind in ("struct", "enum", "trait") and r.file in set(expected_paths)}
                missing_types = [t for t in sorted(named_types) if t not in _defined]
            else:
                missing_types = [t for t in sorted(named_types)
                                 if not defines_type(all_source, t)]
            if len(missing_types) > max(1, len(named_types) // 2):
                return False, (
                    f"types from requirements not defined in generated code "
                    f"({len(missing_types)}/{len(named_types)} missing): "
                    f"{', '.join(missing_types[:5])}"
                )

    return True, "ok"


def validate_task2_api_surface_conformance(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    deps: Task2CheckerDeps | None = None,
) -> tuple[bool, str]:
    """Compare pub items in regenerated source to pre-strip api_surface. Returns (ok, report)."""
    api_text = deps.load_api_surface(project_dir, module_name) if deps is not None else ""
    if not api_text:
        return True, "no api_surface captured — skipping conformance check"

    api_items = _extract_pub_items(api_text)
    if not api_items:
        return True, "api_surface contained no pub items — skipping"

    src_root = module_dir / "src"
    walk_root = src_root if src_root.is_dir() else module_dir
    gen_items = _pub_items_from_tree(walk_root, deps) if deps is not None else set()

    missing = api_items - gen_items
    if not missing:
        return True, f"ok — all {len(api_items)} pub item(s) present in regenerated source"

    by_kind: dict[str, list[str]] = {}
    for kind, name in sorted(missing):
        by_kind.setdefault(kind, []).append(name)
    parts = [f"{kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items())]
    report = (
        f"API drift — {len(missing)} of {len(api_items)} pub item(s) missing from "
        f"regenerated source: " + "; ".join(parts)
    )
    return False, report
