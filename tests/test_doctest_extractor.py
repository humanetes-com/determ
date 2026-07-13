"""Pins for the doc-test harvester (F10 extraction tier 1)."""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourSubKind,
    FenceDirective,
    OutcomeKind,
)
from pipeline_core.domain.doctest import (
    extract_doc_code_blocks,
    harvest_doctest_behaviours,
    harvest_static_assertions,
)

_SRC = '''\
/// Constructs an error.
///
/// ```
/// use anyhow::anyhow;
/// let e = anyhow!("boom");
/// assert_eq!(e.to_string(), "boom");
/// ```
pub fn anyhow() {}

/// A no_run example.
///
/// ```no_run
/// let e = anyhow!("x");
/// # let _hidden = 1;
/// ```
pub fn other() {}

//! ```text
//! not rust
//! ```
'''


def test_extracts_three_blocks_with_correct_fences():
    blocks = extract_doc_code_blocks(_SRC)
    assert len(blocks) == 3
    assert blocks[0].fence is FenceDirective.RUN and blocks[0].is_rust
    assert blocks[1].fence is FenceDirective.NO_RUN
    assert not blocks[2].is_rust  # ```text


def test_hidden_setup_line_kept_without_marker():
    blocks = extract_doc_code_blocks(_SRC)
    assert "let _hidden = 1;" in blocks[1].code
    assert "# let _hidden" not in blocks[1].code


def test_harvest_skips_non_rust_and_empty():
    bs = harvest_doctest_behaviours(_SRC, source_file="src/lib.rs")
    # 2 rust blocks → 2 assertion-behaviours (```text excluded).
    assert len(bs) == 2
    assert all(b.subkind is BehaviourSubKind.ASSERTION for b in bs)
    assert all(b.then is None for b in bs)
    assert bs[0].fence is FenceDirective.RUN
    assert bs[0].source_ref.startswith("src/lib.rs:")


def test_should_panic_and_compile_fail_fences():
    src = ("/// ```should_panic\n/// panic!(\"x\");\n/// ```\n"
           "/// ```compile_fail\n/// let x: u8 = \"s\";\n/// ```\n")
    blocks = extract_doc_code_blocks(src)
    assert blocks[0].fence is FenceDirective.SHOULD_PANIC
    assert blocks[1].fence is FenceDirective.COMPILE_FAIL
    # compile_fail is a negative behaviour — not a positive oracle.
    bs = harvest_doctest_behaviours(src, source_file="f.rs")
    assert not bs[1].is_positive_oracle


def test_harvest_static_assertions_from_doctest():
    src = ("/// ```\n"
           "/// assert_eq!(size_of::<Error>(), size_of::<usize>());\n"
           "/// ```\n")
    bs = harvest_static_assertions(src, source_file="src/lib.rs")
    assert len(bs) == 1
    assert bs[0].then is not None
    assert bs[0].then.kind is OutcomeKind.STATIC_ASSERTION
