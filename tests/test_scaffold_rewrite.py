"""Pins for the F11 scaffold rewrite (authored layout → skeleton)."""
from __future__ import annotations

from pipeline_core.domain.scaffold_rewrite import (
    find_definition_span,
    replace_definition,
)


def test_whitespace_tolerant_span():
    skeleton = "mod ptr {\n    struct Own<T> {\n        ptr: NonNull<T>,\n    }\n}\n"
    enumerated = "struct Own<T> { ptr: NonNull<T>, }"
    span = find_definition_span(skeleton, enumerated)
    assert span is not None
    start, end = span
    assert skeleton[start:end].startswith("struct Own<T>")
    assert skeleton[start:end].rstrip().endswith("}")


def test_replace_swaps_authored_definition():
    skeleton = "struct ErrorImpl {\n    vtable: &'static ErrorVTable,\n}\n"
    old = "struct ErrorImpl { vtable: &'static ErrorVTable, }"
    new = "struct ErrorImpl { inner: Box<dyn StdError + Send + Sync> }"
    res = replace_definition(skeleton, old, new)
    assert res.replaced
    assert "Box<dyn StdError" in res.text
    assert "vtable" not in res.text


def test_missing_definition_is_no_op_flagged():
    res = replace_definition("struct A;", "struct Z {}", "struct Z2 {}")
    assert not res.replaced and res.text == "struct A;"


def test_empty_definition_not_matched():
    assert find_definition_span("anything", "   ") is None
