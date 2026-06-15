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


def test_parse_failed_tests_matches_old_regex() -> None:
    import re

    def old(output: str) -> set[str]:
        failed: set[str] = set()
        for ln in output.splitlines():
            m = re.match(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", ln)
            if m:
                failed.add(m.group(1))
            m = re.match(r"^----\s+(\S+)\s+stdout\s+----", ln)
            if m:
                failed.add(m.group(1))
        return failed

    cases = [
        "test foo::bar ... FAILED\ntest baz ... ok\n---- foo::bar stdout ----",
        "  test indented ... FAILED",            # leading ws → no match (both)
        "test multi word ... FAILED",            # name has space → no match
        "test x ... FAILED trailing junk",       # trailing after FAILED ok
        "testfoo ... FAILED",                    # no ws after 'test' → no match
        "---- only::section stdout ----",        # section header only
        "----  pad::name  stdout  ----",         # multi-space separators
        "test a ... ok\ntest b ... FAILED\n---- b stdout ----\n---- c stdout ----",
        "test result: FAILED. 1 passed",         # summary line, not a test row
        "",
        "test doc/lib.rs - thing (line 2) ... FAILED",  # doc-test name w/ spaces
    ]
    for c in cases:
        assert test_results.parse_failed_tests(c) == old(c), c


def test_parse_self_note() -> None:
    p = llm_output.parse_self_note
    # last non-empty line, stripped of fences/quotes
    assert p("blah\n  `the real note about lifetimes here`  ") == "the real note about lifetimes here"
    assert p('"a sufficiently long quoted correction note"') == "a sufficiently long quoted correction note"
    # declined / too short / empty → None
    assert p("NONE") is None
    assert p("none") is None
    assert p("too short") is None
    assert p("") is None
    assert p("   \n  \n") is None
    # cap at max_len
    assert len(p("x" * 500)) == 300
    # multi-line: only the last non-empty line is taken
    assert p("first long enough line here\nsecond final note long enough") == "second final note long enough"


def test_parse_retrospective_matches_old_regex() -> None:
    import re

    def old(result: str) -> dict:
        cat = ""
        lines = (result or "").splitlines()
        guidance_lines: list[str] = []
        in_guidance = False
        for ln in lines:
            s = ln.strip().lstrip("*").lstrip("-").lstrip("•").strip()
            s_clean = re.sub(r"^\*+\s*|\s*\*+$", "", s)
            m = re.match(r"(?i)^category\s*[:=]\s*(\S+)", s_clean)
            if m:
                cat = m.group(1).strip().rstrip(".,;").upper()
                in_guidance = False
                continue
            m = re.match(r"(?i)^guidance\s*[:=]\s*(.+)$", s_clean)
            if m:
                guidance_lines = [m.group(1).strip()]
                in_guidance = True
                continue
            if in_guidance and s_clean:
                if re.match(r"(?i)^(category|guidance|note|reason)\s*[:=]", s_clean):
                    in_guidance = False
                else:
                    guidance_lines.append(s_clean)
        guid = " ".join(guidance_lines).strip()
        if not cat and not guid and (result or "").strip():
            guid = (result or "").strip()[:1200]
        return {"category": cat, "guidance": guid,
                "signal": "ok" if (cat or guid) else "unparseable"}

    cases = [
        "",
        "CATEGORY: WRONG_TYPE_CHOICE\nGUIDANCE: use NonNull instead of Box",
        "category = api_hallucination.\nguidance = the method foo() does not exist",
        "**CATEGORY**: TRAIT_BOUND_VIOLATION\n**GUIDANCE**: add where T: Sized\nmore detail here",
        "- CATEGORY: GENUINELY_HARD\n- GUIDANCE: multiple issues\nNOTE: stop",
        "just some freeform text with no labels at all",
        "GUIDANCE: first line\nsecond line\nREASON: x",
        "category:\nguidance:",  # no tokens — both regexes fail
        "CATEGORY: WRONG_DEREF_PATTERN",  # category only
        "x" * 1500,  # cap fallback
    ]
    for c in cases:
        assert llm_output.parse_retrospective(c) == old(c), repr(c)


def test_extract_code_blocks_matches_old_regex() -> None:
    r"""P3.4: fenced code-block extractor equals the old inline
    re.findall(r'```[a-zA-Z0-9_-]*\n(.*?)```', resp, re.DOTALL)."""
    import re
    old = re.compile(r'```[a-zA-Z0-9_-]*\n(.*?)```', re.DOTALL)
    samples = [
        "before ```rust\nlet x = 1;\n``` after",
        "```\nno lang\n```",
        "```py-3_2\na\nb\n```rest```c\n```",
        "no fences here",
        "```rust missing newline after lang``` x",
        "text ```\nfirst\n``` mid ```rs\nsecond\n``` end",
    ]
    for s in samples:
        assert llm_output.extract_code_blocks(s) == old.findall(s), s


def test_strip_uncovered_markers_matches_old_regex() -> None:
    r"""P3.4: marker strip equals old re.sub(r'\s*//\s*!!\s*UNCOVERED[^\n]*', '', t)."""
    import re
    old = re.compile(r'\s*//\s*!!\s*UNCOVERED[^\n]*')
    samples = [
        "pub fn a() {}  // !! UNCOVERED\n",
        "let x = 1;  // !! UNCOVERED guarded by `y > 0`\nlet z = 2;\n",
        "no markers at all\n",
        "code //  !!  UNCOVERED extra spaces\n",
        "a  // !! UNCOVERED\nb  // !! UNCOVERED\n",
    ]
    for s in samples:
        assert llm_output.strip_uncovered_markers(s) == old.sub('', s), s


def test_strip_think_blocks_matches_old_regex() -> None:
    r"""P3.7: think-block strip equals old re.sub(r'<think>.*?</think>', '', t, DOTALL)."""
    import re
    old = re.compile(r'<think>.*?</think>', re.DOTALL)
    cases = [
        "<think>reason</think>\nanswer",
        "a<think>x\ny\nz</think>b<think>w</think>c",
        "no think here",
        "<think>only</think>",
        "unclosed <think>tag stays",
    ]
    for c in cases:
        assert text.strip_think_blocks(c) == old.sub('', c), c


def test_rustc_error_signatures_matches_old_regex() -> None:
    import re
    old = lambda o: frozenset(re.findall(r'error\[E\d+\]: .+', o))  # noqa: E731
    cases = [
        "error[E0277]: the trait bound is not satisfied\n  --> src/a.rs:1",
        "warning: x\nerror[E0599]: no method\nerror[E0277]: bound",
        "error[Eabc]: malformed then error[E12]: real",
        "error[E0001]: ",          # empty message → the greedy `.+` fails
        "no errors", "",
    ]
    for c in cases:
        assert test_results.rustc_error_signatures(c) == old(c), c


def test_parse_rustc_compile_errors_matches_old_regex() -> None:
    import re
    OLD = re.compile(r"^error(?:\[E\d+\])?:\s+([^\n]+)\n\s*-->\s*([^:]+):(\d+)(?::\d+)?",
                     re.MULTILINE)
    old = lambda o: [(m.group(1), m.group(2), int(m.group(3))) for m in OLD.finditer(o)]  # noqa: E731
    cases = [
        "error[E0277]: trait bound\n   --> src/a.rs:12:5\n",
        "error: mismatched types\n  --> src/b.rs:3\n",
        "error[E0599]: m\n --> lib.rs:1:1\nerror[E0277]: x\n --> lib.rs:2\n",
        "error[E0277]: no arrow\nblah", "error[Eabc]: bad\n --> a:1", "no errors",
    ]
    for c in cases:
        assert test_results.parse_rustc_compile_errors(c) == old(c), c


def test_strip_trailing_file_tag_matches_old_regex() -> None:
    import re
    old = lambda t: re.sub(r'\s*</FILE>\s*$', '', t).strip()  # noqa: E731
    for t in ["code\n</FILE>\n", "code</FILE>", "a</FILE>b</FILE>", "plain", "  sp  ", "</FILE>"]:
        assert text.strip_trailing_file_tag(t) == old(t), repr(t)
