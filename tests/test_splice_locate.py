"""Pins the source-splice/locate cluster at its new homes (2026-06-29 relocation):
pure locate/pub-check/impl-render in domain, file-mutating splices in
application/reconstruct/splice.py. Behaviour byte-equivalent to the old
pipeline.py helpers (also covered through the pipeline.* shims in smoke)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.splice import (
    replace_fn_signature,
    splice_fn_body,
)
from pipeline_core.domain.impl_block_ranges import render_impl_block
from pipeline_core.domain.llm_output import extract_signature_block
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_fn_headers import is_pub_fn, locate_fn

SRC = "pub fn get() -> i32 { 1 }\nfn read() -> i32 { unimplemented!() }\n"


def test_locate_fn_offsets() -> None:
    sig, sig_start, body_start, body_end = locate_fn(SRC, "read")
    assert sig == "fn read() -> i32"
    assert SRC[body_start:body_end].strip() == "unimplemented!()"
    assert SRC[sig_start:].startswith("fn read")


def test_locate_fn_absent() -> None:
    assert locate_fn(SRC, "nope") is None


def test_locate_fn_malformed_braces() -> None:
    # opening brace never closes → depth != 0 → None
    assert locate_fn("fn broken() { let x = 1;", "broken") is None


def test_is_pub_fn() -> None:
    assert is_pub_fn(SRC, "get") is True
    assert is_pub_fn(SRC, "read") is False
    assert is_pub_fn(SRC, "absent") is False


def test_extract_signature_block() -> None:
    assert extract_signature_block("x <SIGNATURE> fn read() </SIGNATURE> y") == "fn read()"
    assert extract_signature_block("<SIGNATURE>  </SIGNATURE>") is None
    assert extract_signature_block("no tag") is None


def test_render_impl_block_full_and_fallback() -> None:
    src = "impl Foo {\n    fn a(&self) -> i32 { 1 }\n}\n"
    anchor = src.index("fn a")
    member = FnRecord(fn_name="a", sig_text="fn a(&self) -> i32", sig_byte_start=anchor)
    assert render_impl_block(src, [member]).startswith("impl Foo {")
    assert render_impl_block(src, []) == ""
    # unresolved anchor → signature fallback
    miss = FnRecord(fn_name="a", sig_text="fn a(&self) -> i32", sig_byte_start=9999)
    assert render_impl_block(src, [miss]) == "fn a(&self) -> i32"


def test_splice_fn_body_stub_only(tmp_path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    ok, delta = splice_fn_body(f, "read", "a + 1")
    assert ok and delta != 0 and "a + 1" in f.read_text()
    # already filled → refused unless allow_nonstub
    assert splice_fn_body(f, "read", "a + 2") == (False, 0)
    assert splice_fn_body(f, "read", "a + 2", allow_nonstub=True)[0] is True


def test_replace_fn_signature_private_only(tmp_path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    assert replace_fn_signature(f, "read", "fn read() -> i64") is True
    assert "i64" in f.read_text()
    # pub fn refused; rename refused
    assert replace_fn_signature(f, "get", "fn get() -> i64") is False
    assert replace_fn_signature(f, "read", "fn other() -> i64") is False
