"""Behavioural unit tests for the pure domain layer (CODE_ARCHITECTURE.md §8 R4.2).

These functions are the parsers/transformers contributors most often break: model-output
extraction, prompt-budget truncation, and test-result parsing. Pure (no I/O, deterministic),
so they get real input -> output assertions here rather than fakes-wiring smoke coverage.
"""

from __future__ import annotations

from pipeline_core.domain import llm_output, test_results, text

# --------------------------------------------------------------------------- text


def test_strip_markdown_fences_opening_and_closing() -> None:
    assert text.strip_markdown_fences("```rust\nfn f() {}\n```") == "fn f() {}"
    assert text.strip_markdown_fences("```\ncode\n```") == "code"


def test_strip_markdown_fences_plain_and_doccomment_unaffected() -> None:
    assert text.strip_markdown_fences("plain text") == "plain text"
    # Rust doc-comment fences start with `///`, so the standalone-fence stripper must NOT
    # touch them (else doctests get mangled).
    src = "/// ```\n/// let x = 1;\n/// ```"
    assert text.strip_markdown_fences(src) == src


def test_extract_code_content_strips_think_and_fence() -> None:
    assert text.extract_code_content("<think>reasoning</think>\n```rust\ncode\n```") == "code"
    assert text.extract_code_content("no fence here") == "no fence here"


def test_trim_to_line_cuts_at_newline_never_midline() -> None:
    assert text.trim_to_line("short", 100) == "short"          # under cap: unchanged
    out = text.trim_to_line("aaaa\nbbbb\ncccc", 6)             # over cap: cut at last \n < 6
    assert out == "aaaa\n// ... (truncated)"


def test_smart_truncate_keeps_head_and_tail() -> None:
    assert text.smart_truncate("short", total_limit=100) == "short"
    out = text.smart_truncate("a" * 100, total_limit=20, head=5, tail=5)
    assert out.startswith("aaaaa") and out.endswith("aaaaa")
    assert "90 chars omitted" in out


# --------------------------------------------------------------------- llm_output


def test_extract_files_single_and_multiple() -> None:
    assert llm_output.extract_files('<FILE path="a.rs">fn x() {}</FILE>') == {"a.rs": "fn x() {}"}
    out = llm_output.extract_files('<FILE path="a.rs">A</FILE><FILE path="b.rs">B</FILE>')
    assert out == {"a.rs": "A", "b.rs": "B"}


def test_extract_files_normalises_quotes_and_markdown_prefix() -> None:
    assert llm_output.extract_files("<FILE path='b.rs'>code</FILE>") == {"b.rs": "code"}
    assert llm_output.extract_files('### <FILE path="c.rs">code</FILE>') == {"c.rs": "code"}


def test_extract_files_strips_inner_fence_and_rejects_prose() -> None:
    assert llm_output.extract_files('<FILE path="e.rs">```rust\nfn y(){}\n```</FILE>') == {"e.rs": "fn y(){}"}
    # Prose inside a source-file block is dropped (model wrote a description, not code).
    assert llm_output.extract_files('<FILE path="d.rs">This module does things.</FILE>') == {}


def test_normalize_file_markers() -> None:
    assert llm_output._normalize_file_markers('### <FILE path="x">c</FILE>') == '<FILE path="x">c</FILE>'
    assert llm_output._normalize_file_markers('### FILE path="x">c</FILE>') == '<FILE path="x">c</FILE>'
    assert llm_output._normalize_file_markers("<FILE path='x'>c</FILE>") == '<FILE path="x">c</FILE>'


def test_looks_like_prose() -> None:
    assert llm_output._looks_like_prose("This module implements foo", "a.rs") is True
    assert llm_output._looks_like_prose("fn main() {}", "a.rs") is False
    # Non-source extension: never treated as prose (could be a real .md/.txt artefact).
    assert llm_output._looks_like_prose("This is prose", "notes.txt") is False


def test_extract_genesis_delimited_unclosed_file_and_none() -> None:
    assert llm_output._extract_genesis("x ===GENESIS_START===\nhello world\n===GENESIS_END=== y") == "hello world"
    # Start delimiter but no end → use the rest (only when > 100 chars, to avoid noise).
    assert llm_output._extract_genesis("===GENESIS_START===\n" + "g" * 150) == "g" * 150
    # Fallback to a <FILE path="genesis.md"> block when delimiters absent.
    assert llm_output._extract_genesis('<FILE path="genesis.md">the content</FILE>') == "the content"
    assert llm_output._extract_genesis("nothing genesis-shaped here") is None


# -------------------------------------------------------------------- test_results


def test_count_passing_tests_across_formats() -> None:
    assert test_results.count_passing_tests("test result: ok. 5 passed; 0 failed") == 5
    assert test_results.count_passing_tests("running 0 tests\n... 42 passed") == 42
    assert test_results.count_passing_tests("no recognisable count") == 0


def test_parse_test_counts_sums_across_binaries() -> None:
    out = "test result: ok. 5 passed; 1 failed; 2 ignored\ntest result: ok. 3 passed; 0 failed"
    assert test_results._parse_test_counts(out) == (8, 1, 2)
    assert test_results._parse_test_counts("") == (0, 0, 0)


def test_validate_task3_strict_pass_fail_incomplete() -> None:
    assert test_results.validate_task3_output_strict(5, 0, 0, 5, "stable")[0] == "pass"
    assert test_results.validate_task3_output_strict(3, 2, 0, 5, "stable")[0] == "fail"
    assert test_results.validate_task3_output_strict(2, 0, 0, 5, "stable")[0] == "incomplete"
    assert test_results.validate_task3_output_strict(0, 0, 0, 0, "stable")[0] == "incomplete"


def test_extract_test_output_from_file_strips_genesis_header() -> None:
    blob = "genesis snapshot text\n=== test output ===\nreal cargo output here"
    assert test_results.extract_test_output_from_file(blob) == "real cargo output here"
    # No separator → return the content unchanged.
    assert test_results.extract_test_output_from_file("just output") == "just output"


def test_per_file_test_stats_name_attribution() -> None:
    """Per-file test attribution is name-based (binary outputs interleave on
    stdout, so section↔header order-pairing is unreliable — observed live):
    module paths → src files (incl. dotted gen_tests siblings), bare names →
    the caller-built index, doc-tests → their own path, ambiguous/unknown →
    (unmapped). Ignored tests counted separately."""
    known = {"src/lib.rs", "src/context.rs", "src/context.gen_tests_1.rs",
             "tests/test_fmt.rs", "tests/test_macros.rs"}
    index = test_results.build_test_name_index({
        "tests/test_fmt.rs": "fn helper() {}\n#[test]\nfn test_display() {}\n",
        "tests/test_macros.rs": "pub fn test_ensure() {}\nfn test_display() {}\n",
    })
    assert index["test_ensure"] == "tests/test_macros.rs"
    assert index["test_display"] == ""  # defined in both files → ambiguous
    out = "\n".join([
        "running 3 tests",  # headers/sections deliberately NOT consistent
        "test context::gen_tests_1::test_a ... ok",
        "test context::tests::test_b ... FAILED",
        "test test_ensure ... ok",
        "test test_display ... ok",
        "test ui ... ignored, requires nightly",
        "test src/lib.rs - Error (line 10) ... ok",
        "test result: FAILED. 3 passed; 1 failed; 1 ignored",
        "--- stderr ---",
        "     Running unittests src/lib.rs (target/debug/deps/x-abc)",
    ])
    stats = test_results.per_file_test_stats(out, known, index)
    assert stats["src/context.gen_tests_1.rs"] == {"passed": 1, "failed": 0, "ignored": 0}
    assert stats["src/context.rs"] == {"passed": 0, "failed": 1, "ignored": 0}  # inline mod tests
    assert stats["tests/test_macros.rs"] == {"passed": 1, "failed": 0, "ignored": 0}
    assert stats["src/lib.rs"] == {"passed": 1, "failed": 0, "ignored": 0}  # doc-test
    # ambiguous bare name + unknown bare name ("ui" not indexed) → unmapped
    assert stats["(unmapped)"] == {"passed": 1, "failed": 0, "ignored": 1}
    assert test_results.per_file_test_stats("no tests here", known, index) == {}
