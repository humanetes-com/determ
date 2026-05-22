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
import re  # noqa: F401  — used by individual test bodies
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


def test_retrospective_prompt_includes_failed_attempts() -> None:
    """When unproductive_blocks is non-empty, the retrospective prompt must
    surface them as concrete evidence so the LLM can revise a TESTABLE call.
    We don't actually call the LLM — we monkey-patch `ask` and inspect the
    prompt string the retrospective would have sent."""
    import pipeline

    captured: dict = {}

    def fake_ask(prompt, **_kw):
        captured['prompt'] = prompt
        return "FUNCTION foo: UNREACHABLE — based on the prior failed attempts.\n"

    original_ask = pipeline.ask
    pipeline.ask = fake_ask
    try:
        result = pipeline._task0c_retrospective(
            rel_path="src/foo.rs",
            existing_text="pub fn foo() {\n    // !! UNCOVERED\n}\n",
            fn_bodies="pub fn foo() {\n    // !! UNCOVERED\n}\n",
            old_block="mod gen_tests_1 { #[test] fn t() { foo(); } }",
            module_name="tiny",
            unproductive_blocks=[
                'mod gen_tests_1 { #[test] fn a() { std::env::set_var("X", "1"); } }',
                'mod gen_tests_2 { #[test] fn b() { std::env::set_var("X", "1"); } }',
            ],
        )
    finally:
        pipeline.ask = original_ask

    prompt = captured['prompt']
    assert "PRIOR FAILED ATTEMPTS" in prompt
    assert "attempt 1" in prompt and "attempt 2" in prompt
    assert "did NOT improve coverage" in prompt
    assert "the classification was\nwrong" in prompt or "wrong" in prompt
    assert "set_var" in prompt, "concrete attempt content should appear in the prompt"
    assert "UNREACHABLE" in result, "fake LLM response should be returned"

    # Without unproductive_blocks, no PRIOR FAILED ATTEMPTS section.
    captured.clear()
    pipeline.ask = fake_ask
    try:
        pipeline._task0c_retrospective(
            rel_path="src/foo.rs",
            existing_text="pub fn foo() {}\n",
            fn_bodies="pub fn foo() {}\n",
            old_block="",
            module_name="tiny",
        )
    finally:
        pipeline.ask = original_ask
    assert "PRIOR FAILED ATTEMPTS" not in captured['prompt']

    print("  OK  retrospective_prompt_includes_failed_attempts")


def test_api_surface_conformance() -> None:
    """Capture api_surface, mutate the regenerated source, and verify the
    conformance check reports the missing pub items."""
    import pipeline

    workspace = _setup_workspace("smoke-api-conformance")
    module_name = "tiny"
    pipeline._capture_api_surface(workspace, workspace, module_name)

    # All pub items present → ok.
    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace, module_name, workspace
    )
    assert ok, f"unmodified fixture should conform: {report}"
    assert "ok" in report.lower()

    # Drop a public function from the source. Conformance should flag it.
    src = workspace / "src" / "lib.rs"
    text = src.read_text()
    # Remove the never_called fn entirely.
    text_mutated = re.sub(
        r'pub fn never_called\(\) -> i32 \{\s*42\s*\}\s*',
        '',
        text,
    )
    assert text_mutated != text, "test setup: expected to remove never_called"
    src.write_text(text_mutated)

    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace, module_name, workspace
    )
    assert not ok, "expected drift report after removing pub fn"
    assert "never_called" in report
    assert "fn:" in report

    # Missing api_surface → check is a no-op.
    workspace2 = _setup_workspace("smoke-api-conformance-no-api")
    ok, report = pipeline.validate_task2_api_surface_conformance(
        workspace2, module_name, workspace2
    )
    assert ok
    assert "no api_surface" in report.lower()

    print("  OK  validate_task2_api_surface_conformance")


def test_examples_collector() -> None:
    """collect_examples picks up examples/*.rs files for TASK 1 usage signal."""
    import pipeline

    workspace = _setup_workspace("smoke-examples")
    examples = pipeline.collect_examples(workspace)
    rels = [rel for rel, _ in examples]
    assert any("examples/usage.rs" in r for r in rels), \
        f"expected examples/usage.rs in collected examples, got: {rels}"

    # Sanity: content is the actual example body, not empty.
    _, content = examples[0]
    assert "fn main" in content
    assert "add(" in content

    # collect_test_files does NOT pick up examples (they aren't tests).
    test_files = pipeline.collect_test_files(workspace)
    assert not any("examples/" in rel for rel, _ in test_files), \
        "examples/ files must NOT appear in collect_test_files output"

    print("  OK  collect_examples")


def test_decl_only_src_noise_filter() -> None:
    """Stripped src files containing only gen_tests decls should be skipped by
    TASK 1's input collector. Regression for the post-strip noise issue where
    src/lib.rs would survive strip as a 4-line decl stub and confuse TASK 1."""
    import pipeline

    decl_only = (
        '// auto-generated stub after strip\n'
        '\n'
        '#[cfg(test)]\n'
        '#[allow(clippy::all)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
        '\n'
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_2.rs"]\n'
        'mod gen_tests_2;\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(decl_only) is True

    decl_only_no_comments = (
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(decl_only_no_comments) is True

    # A real source file with actual code is NOT decl-only.
    has_code = (
        'pub fn add(a: i32, b: i32) -> i32 { a + b }\n'
        '#[cfg(test)]\n'
        'mod inline_tests { #[test] fn t() {} }\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(has_code) is False

    # A stripped file with one inline test mod (not a path-mod decl) is NOT decl-only.
    has_inline_tests = (
        '#[cfg(test)]\n'
        'mod tests {\n'
        '    #[test] fn t() {}\n'
        '}\n'
    )
    assert pipeline._is_gen_tests_decl_only_src(has_inline_tests) is False

    # Empty / whitespace counts as decl-only (no semantic content).
    assert pipeline._is_gen_tests_decl_only_src("") is True
    assert pipeline._is_gen_tests_decl_only_src("\n\n  \n") is True

    print("  OK  _is_gen_tests_decl_only_src")


def test_has_gen_tests_decl() -> None:
    """The guard used by task2 reattach: detects whether src contains
    `mod gen_tests_<n>;`. Regression coverage for the orphan-siblings bug
    where the previous guard checked only sibling-file existence."""
    import pipeline

    src = (
        'pub fn add(a: i32, b: i32) -> i32 { a + b }\n'
        '#[cfg(test)]\n'
        '#[allow(clippy::all)]\n'
        '#[path = "lib.gen_tests_1.rs"]\n'
        'mod gen_tests_1;\n'
        '#[cfg(test)]\n'
        '#[path = "lib.gen_tests_3.rs"]\n'
        'mod gen_tests_3;\n'
    )
    assert pipeline._has_gen_tests_decl(src, 1) is True
    assert pipeline._has_gen_tests_decl(src, 3) is True
    assert pipeline._has_gen_tests_decl(src, 2) is False, "missing decl must report False"
    assert pipeline._has_gen_tests_decl("", 1) is False
    assert pipeline._has_gen_tests_decl("// no decls here\n", 1) is False

    print("  OK  _has_gen_tests_decl")


def test_api_surface_capture() -> None:
    """Capture pre-strip API surface from the fixture and validate the artifact."""
    import pipeline

    workspace = _setup_workspace("smoke-api-surface")
    module_name = "tiny"
    out = pipeline._capture_api_surface(workspace, workspace, module_name)
    assert out is not None, "API capture must return a path for the fixture"
    assert out.exists()
    assert out.parent.name == pipeline.ORIGINAL_STRUCTURE_DIR

    text = out.read_text()
    # Must include each public function as a signature, with body elided.
    assert "fn add" in text, "missing add() signature"
    assert "fn divide" in text, "missing divide() signature"
    assert "fn never_called" in text, "missing never_called() signature"
    # Function bodies should be elided in the condensed view (no implementation).
    assert "a + b" not in text, "function body leaked into API surface"
    # Header / role text must be present so the LLM knows what this is.
    assert "API surface" in text
    assert "ground truth" in text.lower()

    # load_api_surface round-trip.
    loaded = pipeline.load_api_surface(workspace, module_name)
    assert loaded == text

    # _format_api_surface_section wraps it with the prompt header and truncates.
    section = pipeline._format_api_surface_section(loaded)
    assert "Public API surface" in section
    assert "ground truth" in section.lower()
    # Empty input → empty section (no prompt noise).
    assert pipeline._format_api_surface_section("") == ""

    print("  OK  api_surface capture + load + format")


def test_module_augment_state() -> None:
    """Cover every transition in the augmentation state machine."""
    import pipeline

    # 1. Done — coverage hits 100.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(100.0, 50, False)
    assert s.status("lib") == "done"

    # 2. Done — proc-macro crates short-circuit regardless of coverage.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(12.0, 1, False)
    assert s.status("proc-macro") == "done"

    # 3. Ceiling — at_ceiling=True maps to 'ceiling' (used to accept the module).
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(45.0, 12, True)
    assert s.status("lib") == "ceiling"

    # 4. Retry — first non-improving pass below ceiling, still under stall limit.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(80.0, 80, False)   # baseline
    s.record_pass(80.0, 80, False)   # no improvement; stall=1
    assert s.status("lib") == "retry"
    assert s.stall_passes == 1
    assert s.best_covered_lines == 80

    # 5. Stall counter resets on improvement.
    s.record_pass(82.0, 82, False)   # improved
    assert s.stall_passes == 0
    assert s.best_covered_lines == 82

    # 6. Hard block — STALL_LIMIT consecutive no-improvement passes.
    s = pipeline.ModuleAugmentState("m", "stable")
    s.record_pass(80.0, 80, False)
    for _ in range(pipeline.ModuleAugmentState.STALL_LIMIT):
        s.record_pass(80.0, 80, False)
    assert s.stall_passes == pipeline.ModuleAugmentState.STALL_LIMIT
    assert s.status("lib") == "hard_block"

    # 7. Coverage reaching 100 wins over hard_block (done takes precedence).
    s = pipeline.ModuleAugmentState("m", "stable")
    for _ in range(pipeline.ModuleAugmentState.STALL_LIMIT + 1):
        s.record_pass(99.0, 99, False)
    assert s.status("lib") == "hard_block"
    s.record_pass(100.0, 100, False)
    assert s.status("lib") == "done"

    print("  OK  ModuleAugmentState transitions")


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
    test_examples_collector()
    test_decl_only_src_noise_filter()
    test_has_gen_tests_decl()
    test_api_surface_capture()
    test_api_surface_conformance()
    test_retrospective_prompt_includes_failed_attempts()
    test_module_augment_state()

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
