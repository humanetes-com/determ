"""Source-strip adapter (ADAPTER: filesystem mutation + the strip_to_tests
backend). Moved verbatim from pipeline.py. Removes a single module's
implementation source while preserving test content, recording the original
file list first so TASK 2 has a deterministic file plan.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.discovery import SKIP_DIR_NAMES
from pipeline_core.log import log as _log
from strip_to_tests import detect_language, process_rust

# Directory (at the project root, survives strip) holding the pre-strip
# original-structure manifests + API surface snapshots.
ORIGINAL_STRUCTURE_DIR = ".original_structure"


def save_original_structure(project_dir: Path, module_dir: Path) -> None:
    """Save list of .rs source files in module_dir before stripping."""
    module_rel = str(module_dir.relative_to(project_dir))
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    out_dir.mkdir(exist_ok=True)
    files = sorted(
        str(p.relative_to(project_dir))
        for p in module_dir.rglob("*.rs")
        if not any(part in SKIP_DIR_NAMES for part in p.relative_to(project_dir).parts)
        and p.is_file()
    )
    (out_dir / f"{module_rel}.txt").write_text("\n".join(files))
    _log(f"  Saved original structure: {len(files)} source file(s) recorded.")


def strip_module_source(project_dir: Path, module_dir: Path) -> None:
    """Strip implementation source from a single module directory, leaving test
    content intact. Equivalent to running strip_to_tests on just this subtree.
    Other modules on disk are not touched — their real source remains available.

    Saves the original source file list to .original_structure/<module>/files.txt
    BEFORE stripping, so TASK 2 can use it as a deterministic file plan instead
    of asking the LLM to guess what files to create.
    """
    lang = detect_language(project_dir)
    if lang == "rust":
        # Save original file list before any files are deleted.
        save_original_structure(project_dir, module_dir)
        _log(f"  Stripping source from {module_dir.relative_to(project_dir)}/...")
        process_rust(module_dir, dry_run=False)
    else:
        _log(f"  WARNING: strip not supported for language '{lang}' — skipping.")
