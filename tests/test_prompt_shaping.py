"""prompts.shaping — relocated test-content prompt helpers."""
from __future__ import annotations

from pipeline_core.prompts.shaping import (
    chunk_test_files,
    chunk_test_files_by_file,
    format_test_files,
    prompt_budget,
    split_at_semantic_boundaries,
)


def test_prompt_budget_floor_and_subtraction() -> None:
    assert prompt_budget(100000, "x" * 100) == 100000 - 100
    assert prompt_budget(1000, "x" * 5000) == 2000   # never below the 2,000 floor


def test_format_test_files() -> None:
    assert format_test_files([("a.rs", "A"), ("b.rs", "B")]) == "=== a.rs ===\nA\n\n=== b.rs ===\nB"


def test_chunk_by_file_one_chunk_per_small_file() -> None:
    chunks = chunk_test_files_by_file([("a.rs", "x"), ("b.rs", "y")], 100000)
    assert chunks == [[("a.rs", "x")], [("b.rs", "y")]]


def test_chunk_packs_small_files_under_limit() -> None:
    chunks = chunk_test_files([("a.rs", "x"), ("b.rs", "y")], 100000)
    assert len(chunks) == 1 and len(chunks[0]) == 2


def test_split_at_semantic_boundaries_prefers_test_fn() -> None:
    # Two short lines accumulate, then a `#[test]` line sits at index>0 in the
    # buffer when the long fn body overflows — so the backward scan breaks AT the
    # `#[test]` boundary (not a hard mid-line split).
    content = "a\nb\n#[test]\nfn t() { yyyyyyyyyy }\n"
    parts = split_at_semantic_boundaries(content, 15)
    assert "".join(parts) == content                 # lossless
    assert len(parts) == 2
    assert parts[0] == "a\nb\n"
    assert parts[1].lstrip().startswith("#[test]")   # broke at the boundary
