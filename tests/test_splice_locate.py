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
    ok, reason, delta = splice_fn_body(f, "read", "a + 1")
    assert ok and reason == "ok" and delta != 0 and "a + 1" in f.read_text()
    # already filled → refused unless allow_nonstub
    assert splice_fn_body(f, "read", "a + 2") == (False, "target_not_stub", 0)
    assert splice_fn_body(f, "read", "a + 2", allow_nonstub=True).ok is True


def test_replace_fn_signature_private_only(tmp_path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    assert replace_fn_signature(f, "read", "fn read() -> i64") is True
    assert "i64" in f.read_text()
    # pub fn refused; rename refused
    assert replace_fn_signature(f, "get", "fn get() -> i64") is False
    assert replace_fn_signature(f, "read", "fn other() -> i64") is False


_ATTRED = (
    'pub fn get() -> i32 { 1 }\n'
    '#[cfg(feature = "std")]\n'
    '    #[cold]\n'
    '    fn read() -> i32 { unimplemented!() }\n'
)


def test_replace_fn_signature_consumes_the_models_attribute_copy(tmp_path) -> None:
    """The write starts at the signature LINE, so re-emitted attributes would
    land BELOW the originals and duplicate them (a duplicated `#[cfg]` silently
    changes which cluster the fn compiles in)."""
    f = tmp_path / "lib.rs"
    f.write_text(_ATTRED)
    assert replace_fn_signature(
        f, "read",
        '#[cfg(feature = "std")]\n#[cold]\nfn read() -> i64') is True
    out = f.read_text()
    assert out.count("#[cold]") == 1
    assert out.count('#[cfg(feature = "std")]') == 1
    assert "    fn read() -> i64 {" in out       # original indent preserved


def test_replace_fn_signature_refuses_a_candidate_carrying_a_body(tmp_path) -> None:
    """A whole item written in front of the existing body duplicates the fn
    (E0255 on the second definition) — refuse instead of splicing."""
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    assert replace_fn_signature(
        f, "read", "fn read() -> i64 { 7 }\nfn read() -> i64 { 8 }") is False
    assert f.read_text() == SRC


def test_splice_fn_body_refuses_a_body_that_escapes_its_braces(tmp_path) -> None:
    """`... }\\npub fn read(...) { ...` balances and parses, but defines a
    SECOND top-level `read` — E0255 that poisons every later fill in the file."""
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    escaping = "1 }\nfn read() -> i32 { 2"
    assert splice_fn_body(f, "read", escaping) == (False, "item_set_changed", 0)
    assert f.read_text() == SRC


def test_splice_fn_body_keeps_a_nested_helper_fn(tmp_path) -> None:
    """A helper declared INSIDE the body sits at depth ≥ 1 — legitimate, and
    the top-level fingerprint is unchanged, so it must still splice."""
    f = tmp_path / "lib.rs"
    f.write_text(SRC)
    ok, reason, _delta = splice_fn_body(f, "read", "fn helper() -> i32 { 3 }\nhelper()")
    assert ok and reason == "ok"
    assert "fn helper()" in f.read_text()
