"""Pins the prompt source-view cluster at its new home
(application/reconstruct/source_view.py, 2026-06-29 relocation)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.source_view import (
    annotate_file_view,
    condense_rust_to_signatures,
)

SRC = (
    "pub fn a() -> i32 {\n"
    "    let x = 1;\n"
    "    x + 1\n"
    "}\n"
    "fn b() -> i32 { unimplemented!() }\n"
)


def test_condense_elides_bodies() -> None:
    out = condense_rust_to_signatures(SRC)
    # the fn signature survives; the multi-line body of `a` is elided
    assert "pub fn a() -> i32" in out
    assert "x + 1" not in out


def test_annotate_marks_target_and_stub() -> None:
    out = annotate_file_view(SRC, "b", doc_keep_full_limit=10_000)
    assert ">>> TARGET" in out
    # `b` is the target; `a` is a real (non-stub) body → no annotation on it
    assert "SKELETON STUB" not in out
    assert "fn b() -> i32" in out


def test_annotate_non_target_stub_gets_skeleton_note() -> None:
    src = "fn x() { unimplemented!() }\nfn y() { unimplemented!() }\n"
    out = annotate_file_view(src, "x", doc_keep_full_limit=10_000)
    assert ">>> TARGET" in out          # x marked target
    assert "SKELETON STUB" in out        # y marked as later-filled stub


def test_annotate_condenses_when_over_limit() -> None:
    # doc_keep_full_limit=0 forces the condense-docs branch; output still valid
    doc = "/// long doc line that would be condensed\npub fn a() -> i32 { 1 }\n"
    out = annotate_file_view(doc, "a", doc_keep_full_limit=0)
    assert "pub fn a() -> i32" in out
