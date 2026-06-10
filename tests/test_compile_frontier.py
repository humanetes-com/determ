"""R8.3: `_compile_error_frontier` builds the compile-error frontier from
STRUCTURED cargo diagnostics (check-only re-run) and regex-parses the
caller's rendered text only as a fallback when no diagnostics came back.
"""
from __future__ import annotations

from pathlib import Path

import pipeline
from tests.harness import diag, install_scripted_cargo

LIB = "pub fn alpha() -> u32 {\n    unimplemented!()\n}\n"


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(LIB)
    recs = [{"file": "src/lib.rs", "fn_name": "alpha",
             "sig_byte_start": 0, "body_byte_end": len(LIB)}]
    monkeypatch.setattr(pipeline, "_enumerate_fns_via_extractor",
                        lambda *a, **k: recs)
    return ws


def test_structured_diags_win_over_text(monkeypatch, tmp_path):
    # Rendered text is ANSI-coloured (the class `_COMPILE_ERROR_RE` cannot
    # match) — attribution must come from the diagnostics, not the text.
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x",
                    [diag("src/lib.rs", line=2, code="E0308")]))
    fr = pipeline._compile_error_frontier(
        "\x1b[31merror\x1b[0m: nope", ws, "cargo test", {}, 5)
    assert fr == {("src/lib.rs", "alpha"): {"<compile>"}}


def test_rc_zero_means_no_compile_frontier(monkeypatch, tmp_path):
    # Crate compiles → no compile frontier, even when the caller's text
    # still contains error-shaped lines (e.g. quoted in test output).
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    out = "error[E0308]: stale line\n --> src/lib.rs:2\n"
    assert pipeline._compile_error_frontier(out, ws, "cargo test", {}, 5) == {}


def test_no_diags_falls_back_to_text_parse(monkeypatch, tmp_path):
    # rc!=0 but zero structured diagnostics (JSON unavailable) → legacy
    # regex parse of the caller's rendered text.
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_cargo(monkeypatch, check_diag=(101, "error: x", []))
    out = "error[E0308]: mismatched types\n  --> src/lib.rs:2:5\n"
    fr = pipeline._compile_error_frontier(out, ws, "cargo test", {}, 5)
    assert fr == {("src/lib.rs", "alpha"): {"<compile>"}}
