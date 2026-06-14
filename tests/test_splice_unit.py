"""Unit coverage for splice.py locate-disambiguation + splice-guard paths,
using a fake enumerator (no syn binary). Complements test_splice_byte_offsets.py
(the byte-correctness regression)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from pipeline_core.application.reconstruct.splice import (
    locate_fn_by_record,
    splice_fn_body_at,
)


def _rec(**kw: object) -> dict[str, object]:
    base = {"file": "src/lib.rs", "fn_name": "f", "container": "",
            "sig_text": "fn f()", "sig_byte_start": 0, "body_byte_start": 5,
            "body_byte_end": 10, "has_unimplemented_body": True}
    base.update(kw)
    return base


def _loc(recs: list[dict[str, object]] | None, name: str = "f",
         container: str = "", sig: str = "fn f()") -> object:
    return locate_fn_by_record(lambda _p: recs, Path("/x/src/lib.rs"),
                               name, container, sig)


def test_locate_none_when_enumeration_fails() -> None:
    assert _loc(None) is None


def test_locate_none_when_no_file_match() -> None:
    assert _loc([_rec(file="src/other.rs")]) is None


def test_locate_none_when_no_name_match() -> None:
    assert _loc([_rec(fn_name="g")]) is None


def test_locate_container_narrows() -> None:
    recs = [_rec(container="impl A", sig_byte_start=1),
            _rec(container="impl B", sig_byte_start=2)]
    out = _loc(recs, container="impl B")
    assert out is not None and out[0] == 2


def test_locate_exact_sig_wins() -> None:
    recs = [_rec(sig_text="fn f() -> i32", sig_byte_start=1),
            _rec(sig_text="fn f()", sig_byte_start=2)]
    out = _loc(recs, sig="fn f()")
    assert out is not None and out[0] == 2


def test_locate_normalised_sig_match() -> None:
    recs = [_rec(sig_text="fn   f( )", sig_byte_start=7)]
    out = _loc(recs, sig="fn f( )")
    assert out is not None and out[0] == 7


def test_locate_ambiguous_picks_stub() -> None:
    recs = [_rec(has_unimplemented_body=False, sig_byte_start=1),
            _rec(has_unimplemented_body=True, sig_byte_start=2)]
    out = _loc(recs)  # two same-name/sig → pick the stub-bodied one
    assert out is not None and out[0] == 2


def test_splice_read_failure_returns_false() -> None:
    ok, delta = splice_fn_body_at(Path("/no/such/file.rs"), 0, 1, "x")
    assert ok is False and delta == 0


def _stub_file(body: str = "    ::core::unimplemented!()\n") -> tuple[Path, int, int]:
    td = Path(tempfile.mkdtemp()) / "lib.rs"
    raw = ("fn f() {\n" + body + "}\n").encode("utf-8")
    td.write_bytes(raw)
    bs = raw.index(b"{") + 1
    be = raw.index(b"}", bs)
    return td, bs, be


def test_splice_out_of_bounds_returns_false() -> None:
    p, bs, _ = _stub_file()
    ok, _ = splice_fn_body_at(p, bs, 10_000, "x")
    assert ok is False


def test_splice_refuses_nonstub_body() -> None:
    p, bs, be = _stub_file(body="    let a = 1;\n")  # real body, not a stub
    ok, _ = splice_fn_body_at(p, bs, be, "a + 1")
    assert ok is False


def test_splice_strips_outer_braces_and_rejects_empty() -> None:
    p, bs, be = _stub_file()
    ok, _ = splice_fn_body_at(p, bs, be, "{ }")  # strips to empty → reject
    assert ok is False


def test_splice_rejects_new_stub_body() -> None:
    p, bs, be = _stub_file()
    ok, _ = splice_fn_body_at(p, bs, be, "unimplemented!()")
    assert ok is False


def test_splice_braced_body_succeeds() -> None:
    p, bs, be = _stub_file()
    ok, delta = splice_fn_body_at(p, bs, be, "{ a + 1 }")
    assert ok is True and delta != 0
    assert "a + 1" in p.read_text()
