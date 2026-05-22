#!/usr/bin/env python3
"""Smoke tests for pipeline coverage measurement and gen_tests helpers.

Two phases:
  - Pure-Python tests (no cargo): verify gen_tests sibling-file mechanics.
    Fast (sub-second), always run.
  - Cargo coverage test: run the configured COVERAGE_BACKEND on
    fixtures/tiny/ and assert the measurement returns sensible numbers.
    Slow (~30-60s), opt-in via --full or the env var SMOKE_FULL=1.

Invocation:
    .venv/bin/python tests/test_pipeline_smoke.py           # pure-python only
    .venv/bin/python tests/test_pipeline_smoke.py --full    # + cargo phase

Exit non-zero on first failure.
"""
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "fixtures" / "tiny"

sys.path.insert(0, str(REPO))


def _setup_workspace(name: str = "fixture-tiny-test") -> Path:
    workspace = REPO / "tmp" / name
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, workspace)
    return workspace


# ─── Pure-Python phase ──────────────────────────────────────────────────────

def test_gen_tests_helpers() -> None:
    """Verify gen_tests sibling-file write / read / revert / strip semantics."""
    import pipeline

    workspace = _setup_workspace("smoke-gen-tests")
    src = workspace / "src" / "lib.rs"
    pristine = src.read_text()

    assert pipeline._gen_tests_count(src) == 0, "expected zero siblings initially"
    assert pipeline._next_gen_tests_n(src) == 1, "next-n should be 1 when empty"

    block_1 = """#[cfg(test)]
#[allow(unused)]
mod gen_tests_1 {
    use super::*;
    #[test]
    fn t() { assert_eq!(add(1, 1), 2); }
}"""
    pipeline._write_gen_tests_pass(src, pristine, block_1, "#[allow(clippy::all)]", 1)
    sibling_1 = pipeline._gen_tests_path(src, 1)
    assert sibling_1.exists(), f"expected sibling {sibling_1}"
    assert sibling_1.name == "lib.gen_tests_1.rs"
    body = sibling_1.read_text()
    assert "use super::*;" in body
    assert "fn t" in body
    # Outer mod wrapper stripped from body.
    assert "mod gen_tests_1" not in body
    # Source got the path-mod decl.
    src_text = src.read_text()
    assert 'mod gen_tests_1;' in src_text
    assert '#[path = "lib.gen_tests_1.rs"]' in src_text
    assert "#[allow(clippy::all)]" in src_text
    assert pipeline._gen_tests_count(src) == 1
    assert pipeline._next_gen_tests_n(src) == 2

    # Read-all re-wraps for LLM context.
    all_text = pipeline._read_all_gen_tests(src)
    assert all_text.startswith("#[cfg(test)]\nmod gen_tests_1 {")

    # Add a second pass.
    block_2 = "mod gen_tests_2 {\n    use super::*;\n    #[test] fn u() {}\n}"
    pipeline._write_gen_tests_pass(src, src.read_text(), block_2, "#[allow(clippy::all)]", 2)
    assert pipeline._gen_tests_count(src) == 2
    assert pipeline._next_gen_tests_n(src) == 3

    # Strip decls from the source text (helper used in many places).
    src_text = src.read_text()
    stripped = pipeline._strip_gen_tests_decls(src_text)
    assert "mod gen_tests_1" not in stripped
    assert "mod gen_tests_2" not in stripped
    assert "pub fn add" in stripped, "original source content must remain"

    # Revert second pass: delete sibling, restore src to the pre-pass text.
    src_before_pass_2 = stripped + "\n" + pipeline._build_gen_tests_decl(
        "lib.gen_tests_1.rs", 1, "#[allow(clippy::all)]"
    )
    sibling_2 = pipeline._gen_tests_path(src, 2)
    pipeline._revert_gen_tests_pass(src, src_before_pass_2, sibling_2)
    assert not sibling_2.exists()
    assert pipeline._gen_tests_count(src) == 1

    # Strip everything.
    n = pipeline._strip_all_gen_tests_artifacts(src)
    assert n == 1
    assert pipeline._gen_tests_count(src) == 0
    final = src.read_text()
    assert "mod gen_tests_1" not in final
    assert "pub fn add" in final

    # is_test_file recognises siblings (so coverage loops skip them).
    fake_sibling = workspace / "src" / "foo.gen_tests_3.rs"
    fake_sibling.write_text("// placeholder\n")
    assert pipeline.is_test_file(fake_sibling, workspace), \
        "is_test_file must return True for gen_tests siblings"
    fake_sibling.unlink()

    print("  OK  gen_tests helpers")


def test_extract_gen_tests_body() -> None:
    import pipeline

    block_with_attrs = """#[cfg(test)]
#[allow(unused)]
mod gen_tests_5 {
    use super::*;

    #[test]
    fn x() {
        assert!(true);
    }
}"""
    body = pipeline._extract_gen_tests_body(block_with_attrs)
    assert body.startswith("use super::*;"), f"body starts wrong: {body[:80]!r}"
    assert "#[test]" in body
    assert "mod gen_tests" not in body
    # Body should be dedented relative to the original 4-space inner indent.
    assert "fn x()" in body
    assert "    fn x()" not in body, "body should be dedented to col 0"

    # Bare functions (no mod wrapper): returned as-is.
    bare = "#[test]\nfn x() { assert!(true); }"
    body = pipeline._extract_gen_tests_body(bare)
    assert "#[test]" in body and "fn x()" in body

    print("  OK  _extract_gen_tests_body")


# ─── Cargo phase (slow, opt-in) ─────────────────────────────────────────────

def test_coverage_measurement() -> None:
    """Run the configured coverage provider on the fixture; assert sensible output."""
    import pipeline

    workspace = _setup_workspace("smoke-coverage")
    cargo_text = (workspace / "Cargo.toml").read_text()
    m = re.search(r'name\s*=\s*"([^"]+)"', cargo_text)
    assert m, "no [package] name in fixture Cargo.toml"
    module_name = m.group(1)

    print(f"  ... running {pipeline.COVERAGE_BACKEND} on {workspace.relative_to(REPO)}")
    pct, files_data, covered, coverable = pipeline._coverage_provider.measure(
        workspace, module_name
    )

    print(f"      coverage: {pct:.1f}% ({covered}/{coverable})")
    assert coverable > 0, "fixture must report coverable lines > 0"
    assert covered > 0, "fixture has tests — covered must be > 0"
    assert covered < coverable, "fixture has uncovered code — partial coverage expected"
    assert 10.0 < pct < 95.0, f"expected 10%-95% coverage, got {pct:.1f}%"

    paths = [fd.get("abs_path") or fd.get("path") or "" for fd in files_data]
    has_lib = any("lib.rs" in (str(p) if not isinstance(p, list) else "/".join(p))
                  for p in paths)
    assert has_lib, f"lib.rs not in measured files_data: {paths}"

    print("  OK  coverage measurement")


def test_coverage_ignores_siblings() -> None:
    """Write a synthetic sibling that would inflate coverage if measured;
    confirm the coverage provider's ignore patterns exclude it."""
    import pipeline

    workspace = _setup_workspace("smoke-cov-sibling")
    src = workspace / "src" / "lib.rs"

    # Plant a sibling with intentionally-uncovered code. If the ignore pattern
    # is wrong, the sibling's lines would show up in `coverable`.
    block = """#[cfg(test)]
mod gen_tests_1 {
    use super::*;
    #[test]
    fn t() {
        let _ = add(0, 0);
        let _ = divide(0, 0);
    }
}"""
    pipeline._write_gen_tests_pass(
        src, src.read_text(), block, "#[allow(clippy::all)]", 1
    )

    cargo_text = (workspace / "Cargo.toml").read_text()
    m = re.search(r'name\s*=\s*"([^"]+)"', cargo_text)
    assert m
    module_name = m.group(1)

    pct, files_data, covered, coverable = pipeline._coverage_provider.measure(
        workspace, module_name
    )
    print(f"  ... with-sibling coverage: {pct:.1f}% ({covered}/{coverable})")

    # The sibling file must not appear in measured files.
    paths = [
        (str(fd.get("abs_path") or fd.get("path") or ""))
        if not isinstance(fd.get("path"), list)
        else "/".join(fd.get("path", []))
        for fd in files_data
    ]
    sibling_seen = any("gen_tests_1" in p for p in paths)
    assert not sibling_seen, (
        f"sibling file leaked into coverage measurement: {paths}"
    )
    print("  OK  coverage ignores siblings")


# ─── Driver ─────────────────────────────────────────────────────────────────

def main() -> int:
    full = "--full" in sys.argv or os.getenv("SMOKE_FULL") == "1"

    print("Pure-Python phase:")
    test_gen_tests_helpers()
    test_extract_gen_tests_body()

    if full:
        print("Cargo phase (--full):")
        test_coverage_measurement()
        test_coverage_ignores_siblings()
    else:
        print("Skipping cargo phase (pass --full or SMOKE_FULL=1 to enable).")

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
