"""F1 pins — transactional, parse-gated fill-tree writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.splice import (
    splice_fn_body,
    splice_fn_body_at,
)
from pipeline_core.application.reconstruct.splice_txn import parse_guarded_write

_REJECT = lambda _p: False   # noqa: E731 — "syn says broken"
_ACCEPT = lambda _p: True    # noqa: E731


def test_parse_guarded_write_commits_when_valid(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"new", b"old", _ACCEPT) is True
    assert f.read_bytes() == b"new"


def test_parse_guarded_write_rolls_back_when_broken(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"BROKEN", b"old", _REJECT) is False
    assert f.read_bytes() == b"old"   # pre-image restored, byte-exact


def test_parse_guarded_write_no_validator_always_commits(tmp_path) -> None:
    f = tmp_path / "a.rs"
    f.write_bytes(b"old")
    assert parse_guarded_write(f, b"new", b"old", None) is True
    assert f.read_bytes() == b"new"


def test_splice_fn_body_rejected_leaves_bytes_untouched(tmp_path) -> None:
    f = tmp_path / "m.rs"
    original = "fn a() {\n    unimplemented!()\n}\n"
    f.write_text(original, encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = 1; x", validate_parse=_REJECT)
    assert out.ok is False and out.reason == "parse_broken"
    assert f.read_text(encoding="utf-8") == original   # rolled back


def test_splice_fn_body_committed_when_parse_ok(tmp_path) -> None:
    f = tmp_path / "m.rs"
    f.write_text("fn a() {\n    unimplemented!()\n}\n", encoding="utf-8")
    out = splice_fn_body(f, "a", "1 + 1", validate_parse=_ACCEPT)
    assert out.ok is True and out.reason == "ok"
    assert "1 + 1" in f.read_text(encoding="utf-8")


def test_splice_fn_body_at_rolls_back(tmp_path) -> None:
    f = tmp_path / "m.rs"
    original = "fn a() { unimplemented!() }\n"
    f.write_bytes(original.encode())
    bs = original.index("unimplemented")
    be = original.index(" }")
    out = splice_fn_body_at(f, bs, be, "0", validate_parse=_REJECT)
    assert out.ok is False and out.reason == "parse_broken"
    assert f.read_bytes() == original.encode()


def test_all_body_splice_writers_expose_the_gate() -> None:
    # Invariant: every fill-tree body-splice writer accepts `validate_parse`, so
    # no model-output write can bypass the parse gate. If a new splice writer is
    # added, wire it through parse_guarded_write and extend this list.
    import inspect

    from pipeline_core.application.reconstruct import splice as _s
    for name in ("splice_fn_body", "splice_fn_body_at", "splice_fn_with_where"):
        sig = inspect.signature(getattr(_s, name))
        assert "validate_parse" in sig.parameters, name


# --- real extractor integration (the true syn signal) ----------------------- #

_BIN = (Path(__file__).parent.parent / "tools" / "skeleton_extractor"
        / "target" / "release" / "skeleton-extractor")


@pytest.mark.skipif(not _BIN.exists(), reason="extractor binary not built")
def test_real_parse_check_catches_broken_splice(tmp_path) -> None:
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.structure import CliStructureReader

    reader = CliStructureReader(CliExtractor(_BIN))
    good = tmp_path / "good.rs"
    good.write_text("fn a() -> u32 { 1 }\n", encoding="utf-8")
    bad = tmp_path / "bad.rs"
    bad.write_text("fn a() -> u32 { let x = ; }\n", encoding="utf-8")
    assert reader.parse_ok(good) is True
    assert reader.parse_ok(bad) is False

    # end-to-end: an invalid body is rejected + bytes restored via the real gate.
    f = tmp_path / "m.rs"
    original = "fn a() -> u32 {\n    unimplemented!()\n}\n"
    f.write_text(original, encoding="utf-8")
    out = splice_fn_body(f, "a", "let x = ; x", validate_parse=reader.parse_ok)
    assert out.reason == "parse_broken"
    assert f.read_text(encoding="utf-8") == original
