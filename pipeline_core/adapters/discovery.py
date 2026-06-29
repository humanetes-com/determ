"""Test/example file discovery (ADAPTER: pure filesystem walks). Moved verbatim
from pipeline.py (P5). The skip-dir / test-dir conventions and the gen_tests
sibling-name parser live here; the one gen_tests-semantics predicate
(`is_gen_tests_decl_only`) is passed explicitly by the caller (composition root)
to the single function that needs it, so the gen_tests family stays in pipeline.py
and this adapter holds no injected module state (#14).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pipeline_core.domain.workspace_layout import SKIP_DIR_NAMES

# Re-export for callers that import from this module (backward-compat).
__all__ = ["SKIP_DIR_NAMES", "TEST_DIR_NAMES", "match_gen_tests_sibling", "collect_test_files"]

# Default predicate for `collect_test_files`'s gen_tests-decl filter — overridden
# by the composition root, which passes the real pipeline implementation.
_NO_GEN_TESTS_DECL: Callable[[str], bool] = lambda _content: False

TEST_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", "test", "spec", "specs", "__tests__", "testing"})

def match_gen_tests_sibling(name: str) -> tuple[str, int] | None:
    """Parse a `<stem>.gen_tests_<N>.rs` filename → (stem, N), or None."""
    if not name.endswith(".rs"):
        return None
    stem, sep, n_str = name[:-3].rpartition(".gen_tests_")
    if not sep or not stem or not n_str.isdigit():
        return None
    return stem, int(n_str)


def is_test_file(path: Path, project_dir: Path) -> bool:
    """Determine if a file is a test file using path and name conventions that
    apply across languages. Does not inspect file contents."""
    rel = path.relative_to(project_dir)

    # Any directory component is a well-known test directory name.
    if any(part.lower() in TEST_DIR_NAMES for part in rel.parts[:-1]):
        return True

    stem = path.stem           # filename without final extension, case preserved
    stem_lower = stem.lower()

    # Files named exactly "test" or "spec" in any language.
    if stem_lower in {"test", "spec", "tests", "specs"}:
        return True

    # snake_case conventions: test_foo.py, foo_test.go
    if stem_lower.startswith("test_") or stem_lower.endswith("_test"):
        return True

    # CamelCase conventions: FooTest.java, FooSpec.rb, FooTests.php
    if stem.endswith(("Test", "Tests", "Spec", "Specs")):
        return True

    # Dot-separated conventions: foo.test.js, foo.spec.ts
    if len(path.suffixes) >= 2 and path.suffixes[-2].lower() in (".test", ".spec"):
        return True

    # Pipeline-generated gen_tests siblings: foo.gen_tests_3.rs
    return match_gen_tests_sibling(path.name) is not None


def collect_test_files(
    project_dir: Path,
    *,
    is_gen_tests_decl_only: Callable[[str], bool] = _NO_GEN_TESTS_DECL,
) -> list[tuple[str, str]]:
    """Return (rel_path, content) pairs for every file containing test code.

    Used by TASK 1 to read the full test spec. Includes:
    - Pure test files: identified by name/location (is_test_file) — e.g. tests/*.rs
    - Rust source files with inline test blocks (#[cfg(test)] or #[test]) — e.g. src/lib.rs

    `is_gen_tests_decl_only` (#14: explicit dep, was module-injected): returns True
    when a stripped .rs file consists only of gen_tests path-mod decls and carries
    no test semantics of its own — those are skipped (their content lives in siblings).

    NOTE: this is the READ set for TASK 1. For the WRITE protection set (files TASK 2
    must never overwrite), use collect_protected_test_files() instead — it excludes
    inline test files in src/ so TASK 2 can regenerate them with new implementation.
    """
    result: list[tuple[str, str]] = []
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        try:
            content = p.read_text()
        except Exception:
            continue
        if is_test_file(p, project_dir):
            result.append((str(rel), content))
        elif p.suffix == ".rs" and ("#[cfg(test)]" in content or "#[test]" in content):
            # Skip stripped source files that contain only gen_tests path-mod
            # declarations — their actual test content lives in sibling files
            # (which are picked up separately by is_test_file).
            if is_gen_tests_decl_only(content):
                continue
            result.append((str(rel), content))
    return result


def collect_examples(
    project_dir: Path, module_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """Return (rel_path, content) for example source files.

    Examples sit in <crate>/examples/ and demonstrate idiomatic API usage. They
    are not assertions — TASK 1 should treat them as usage signal, not as
    compliance targets. Filtered to module_dir when supplied (multi-crate
    workspaces).
    """
    roots = [project_dir] if module_dir is None else [module_dir]
    result: list[tuple[str, str]] = []
    for root in roots:
        ex_dir = root / "examples"
        if not ex_dir.is_dir():
            continue
        for p in sorted(ex_dir.rglob("*.rs")):
            if any(part in SKIP_DIR_NAMES for part in p.relative_to(project_dir).parts):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            result.append((str(p.relative_to(project_dir)), content))
    return result


def collect_protected_test_files(project_dir: Path) -> set[str]:
    """Return rel paths of files TASK 2 must NEVER overwrite.

    Only pure test files: those in tests/ directories or named like test files.
    Excludes source files (src/lib.rs, src/foo.rs) that happen to contain inline
    #[cfg(test)] blocks — those must be regeneratable by TASK 2 so it can write
    the implementation back into them alongside the test blocks.
    """
    result: set[str] = set()
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        if is_test_file(p, project_dir):
            result.add(str(rel))
    return result
