"""Doc tests are tests, and the static inventory could not see them.

task3 announced 149 tests against `expected=124` on the merged anyhow tree and
129 against `expected=104` — undershooting by exactly 25 in both clusters, which
is the number `cargo test --doc` reports on that tree. The `reported < expected`
guard therefore had 25 tests of slack per cluster in the direction it was
written for.
"""
from __future__ import annotations

from pipeline_core.domain.doc_tests import count_doc_tests, doc_lines


def test_a_plain_fenced_block_in_a_doc_comment_is_a_test() -> None:
    assert count_doc_tests(
        "/// Example\n"
        "/// ```\n"
        "/// let x = 1;\n"
        "/// ```\n"
        "pub fn f() {}\n") == 1


def test_a_non_rust_block_is_documentation_not_a_test() -> None:
    """This crate has two `toml` blocks and three `console` blocks; counting
    them read 28 where cargo runs 25."""
    src = (
        "/// ```toml\n/// [dependencies]\n/// ```\n"
        "/// ```console\n/// $ cargo build\n/// ```\n"
        "pub fn f() {}\n")
    assert count_doc_tests(src) == 0


def test_a_non_rust_block_does_not_desynchronise_the_rest_of_the_file() -> None:
    """Its CLOSING fence must still close it. Treating that close as an open
    both over-counted lib.rs and lost two real doc tests in macros.rs."""
    src = (
        "/// ```console\n/// $ cargo build\n/// ```\n"
        "/// ```\n/// let x = 1;\n/// ```\n"
        "pub fn f() {}\n")
    assert count_doc_tests(src) == 1


def test_rustdoc_attributes_are_not_language_tags() -> None:
    for info in ("rust", "no_run", "should_panic", "compile_fail", "edition2021",
                 "rust,no_run"):
        assert count_doc_tests(f"/// ```{info}\n/// x\n/// ```\n") == 1, info


def test_the_doc_attribute_spelling_counts_too() -> None:
    """The pipeline's own syn round-trip renders doc comments as `#[doc = …]`
    attributes, and the reconstructed `ensure!` macro carries two real doc tests
    that way — a scanner reading only `///` loses them silently."""
    src = (
        'macro_rules! __ensure {\n'
        '    ($ensure:item) => {\n'
        '        #[doc = " # Example"] #[doc = ""] #[doc = " ```"]\n'
        '        #[doc = " ensure!(user == 0, \\"only user 0\\");"] #[doc = " ```"]\n'
        '        #[doc = ""] #[doc = " ```"] #[doc = " ensure!(d <= MAX);"]\n'
        '        #[doc = " ```"] $ensure\n'
        '    };\n'
        '}\n')
    assert count_doc_tests(src) == 2


def test_doc_lines_reads_both_spellings_in_source_order() -> None:
    src = '/// first\n#[doc = " second"]\n/// third\n'
    assert [line.strip() for line in doc_lines(src)] == ["first", "second", "third"]
