"""Filesystem I/O for gen_tests sibling files.

Handles reading, writing, reverting, quarantining, and stripping gen_tests_N.rs
sibling files and their `mod gen_tests_N;` path-declarations in parent sources.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.generation import (
    build_gen_tests_decl,
    extract_gen_tests_body,
    gen_tests_sibling_number,
    has_gen_tests_mod_decl,
    parse_gen_tests_filename,
    strip_all_gen_tests_decls,
    strip_gen_tests_decl,
)
from pipeline_core.domain.rust_scan import rust_delimiters_balanced


def gen_tests_siblings(src_file: Path) -> list[tuple[int, Path]]:
    """Return [(N, path)] for all foo.gen_tests_N.rs siblings, sorted by N."""
    stem = src_file.stem
    out: list[tuple[int, Path]] = []
    parent = src_file.parent
    if parent.exists():
        for f in parent.iterdir():
            num = gen_tests_sibling_number(f.name, stem)
            if num is not None:
                out.append((num, f))
    out.sort()
    return out


def gen_tests_count(src_file: Path) -> int:
    return len(gen_tests_siblings(src_file))


def next_gen_tests_n(src_file: Path) -> int:
    """Return the next free gen_tests pass number (max existing + 1, skipping gaps)."""
    siblings = gen_tests_siblings(src_file)
    return (siblings[-1][0] + 1) if siblings else 1


def gen_tests_path(src_file: Path, n: int) -> Path:
    return src_file.with_name(f"{src_file.stem}.gen_tests_{n}.rs")


def read_all_gen_tests(src_file: Path) -> str:
    """Concatenate gen_tests sibling bodies re-wrapped in `mod gen_tests_N { ... }` blocks."""
    parts: list[str] = []
    for n, sibling in gen_tests_siblings(src_file):
        try:
            body = sibling.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        parts.append(f"#[cfg(test)]\nmod gen_tests_{n} {{\n{body}\n}}")
    return "\n\n".join(parts)


def load_gen_tests_origin(project_dir: Path) -> dict[str, str]:
    """Load the gen_tests origin-cfg manifest baked pre-strip.

    Returns {sibling_stem → cluster_cfg_predicate}; '' = stable/default cluster.
    Empty dict if absent (older runs / pre-bake).
    """
    path = project_dir / ".skeleton" / "_gen_tests_origin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def write_gen_tests_pass(
    src_file: Path,
    src_text: str,
    block: str,
    allow_line: str,
    pass_n: int,
    cluster_cfg_attr: str = "",
    log_fn: Callable[[str], None] = lambda s: None,
) -> Path:
    """Write the sibling file and append its decl to src_file.

    Performs a delimiter-balance check; on failure quarantines the sibling without
    updating the parent (bad pass becomes a compile-time no-op). Returns sibling path.
    """
    sibling = gen_tests_path(src_file, pass_n)
    body = extract_gen_tests_body(block)
    if not rust_delimiters_balanced(body):
        quarantine = sibling.with_suffix(".rs.broken")
        quarantine.write_text(body, encoding="utf-8")
        with suppress(Exception):
            log_fn(
                f"  [gen_tests] REJECTED pass {pass_n} for {src_file.name}: "
                f"delimiters unbalanced. Quarantined → {quarantine.name}"
            )
        return sibling
    sibling.write_text(body, encoding="utf-8")
    decl = build_gen_tests_decl(sibling.name, pass_n, allow_line, cluster_cfg_attr=cluster_cfg_attr)
    src_file.write_text(src_text.rstrip() + decl, encoding="utf-8")
    return sibling


def quarantine_broken_gen_tests(
    project_dir: Path, log_fn: Callable[[str], None] = lambda s: None
) -> int:
    """Rename gen_tests siblings with unbalanced delimiters to .rs.broken and strip decls.

    Startup recovery for state from earlier runs. Returns count quarantined.
    """
    moved = 0
    for src in (project_dir / "src").rglob("*.gen_tests_*.rs"):
        try:
            text = src.read_text(encoding="utf-8")
        except Exception:
            continue
        if rust_delimiters_balanced(text):
            continue
        parsed = parse_gen_tests_filename(src.name)
        if parsed is None:
            continue
        stem, pass_n = parsed
        parent = src.parent / f"{stem}.rs"
        try:
            if parent.exists():
                ptext = parent.read_text(encoding="utf-8")
                ptext_new = strip_gen_tests_decl(ptext, src.name, pass_n)
                if ptext_new != ptext:
                    parent.write_text(ptext_new, encoding="utf-8")
        except Exception:
            pass
        try:
            src.rename(src.with_suffix(".rs.broken"))
            moved += 1
        except Exception:
            try:
                src.unlink()
                moved += 1
            except Exception:
                pass
    if moved:
        log_fn(f"  [gen_tests] quarantined {moved} broken sibling(s) at startup.")
    return moved


def revert_gen_tests_pass(src_file: Path, src_text: str, sibling: Path) -> None:
    """Restore src_file to src_text and delete the sibling (and any orphaned siblings).

    R9.6 atomicity: the snapshot may cover multiple siblings; delete every sibling
    whose decl the restore removed, not just the named one.
    """
    src_file.write_text(src_text, encoding="utf-8")
    with suppress(OSError):
        sibling.unlink()
    for _n, _sib in gen_tests_siblings(src_file):
        if not has_gen_tests_mod_decl(src_text, _n):
            with suppress(OSError):
                _sib.unlink()


def strip_all_gen_tests_artifacts(src_file: Path) -> int:
    """Delete all gen_tests siblings and remove all decls from src_file. Returns count deleted."""
    count = 0
    for _, sib in gen_tests_siblings(src_file):
        try:
            sib.unlink()
            count += 1
        except OSError:
            pass
    if src_file.exists():
        original = src_file.read_text(encoding="utf-8")
        cleaned = strip_all_gen_tests_decls(original)
        if cleaned != original:
            src_file.write_text(cleaned, encoding="utf-8")
    return count
