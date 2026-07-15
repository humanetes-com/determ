"""Regression: per-fn splice must use BYTE offsets in byte space — CODE_ARCHITECTURE R12.

The live microcrate run (session 58) corrupted the file when filling `add`: the
splice ate the closing `}` and two `/` of the NEXT fn's `///` doc comment, because
`_splice_fn_body_at` indexed a char `str` with the enumerator's BYTE offsets. The
skeleton's doc comments contain em-dashes (`—`, 3 bytes / 1 char), so byte offset ≠
char index → the slice ran 2 chars long per em-dash. These pin byte-correct splicing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pipeline

# Two fns; a doc comment with an em-dash BEFORE the first → byte offset diverges
# from char index at the splice point (the exact shape that broke `add`).
_SRC = (
    "/// counter — see notes\n"
    "pub fn f() -> i32 {\n"
    "    ::core::unimplemented!()\n"
    "}\n"
    "/// `next` fn\n"
    "pub fn g() -> i32 {\n"
    "    ::core::unimplemented!()\n"
    "}\n"
)


def _byte_body_span(raw: bytes, fn: bytes) -> tuple[int, int]:
    fn_at = raw.index(fn)
    bs = raw.index(b"{", fn_at) + 1
    be = raw.index(b"}", bs)
    return bs, be


def test_splice_fn_body_at_is_byte_correct() -> None:
    raw = _SRC.encode("utf-8")
    bs, be = _byte_body_span(raw, b"fn f")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lib.rs"
        p.write_bytes(raw)
        ok, _reason, _delta = pipeline._APP.fill_roles.mut.splice_fn_body_at(p, bs, be, "a + 1")
        out = p.read_text(encoding="utf-8")

    assert ok is True
    assert "a + 1" in out                  # body filled
    assert "/// `next` fn" in out          # next doc comment INTACT (the bug ate it)
    assert out.count("pub fn") == 2        # nothing merged/dropped
    assert "pub fn g() -> i32 {" in out    # f's `}` preserved → g still parses
    # The em-dash survived intact and the file structure is still valid.
    assert "counter — see notes" in out


def test_splice_fn_body_at_offsets_match_real_extractor() -> None:
    """End-to-end with the syn enumerator (skips without the binary): the offsets
    it emits, fed to the splicer, produce a clean fill — not the off-by-bytes corruption."""
    import shutil

    binp = pipeline.SKELETON_EXTRACTOR_BIN
    if not Path(binp).exists():
        import pytest
        pytest.skip("extractor binary not built")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        src.mkdir()
        (src / "lib.rs").write_bytes(_SRC.encode("utf-8"))
        loc = pipeline._APP.fill_roles.mut.locate_fn_by_record(
            src / "lib.rs", "f", "", "pub fn f() -> i32")
        assert loc is not None
        _, bs, be, _ = loc
        ok, _reason, _ = pipeline._APP.fill_roles.mut.splice_fn_body_at(
            src / "lib.rs", bs, be, "a + 1")
        out = (src / "lib.rs").read_text(encoding="utf-8")
        shutil.rmtree(src, ignore_errors=True)
    assert ok is True
    assert "/// `next` fn" in out
    assert out.count("pub fn") == 2
