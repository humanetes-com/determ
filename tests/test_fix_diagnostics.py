"""Unit tests for the extracted fix-loop diagnostics (P3 slice 1)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct import fix_diagnostics as fd

_OUT = (
    "error[E0277]: the trait bound is not satisfied\n"
    "  --> src/lib.rs:12:5\n"
    "error[E0599]: no method named foo\n"
    "  --> src/util.rs:3:9\n"
    "error[E0277]: another in lib\n"
    "  --> ./src/lib.rs:40:1\n"
)


def test_error_sigs_distinct() -> None:
    sigs = fd.error_sigs(_OUT)
    assert "error[E0277]: the trait bound is not satisfied" in sigs
    assert "error[E0599]: no method named foo" in sigs
    assert len(sigs) == 3  # frozenset of the three distinct error-sig lines


def test_errors_by_file_counts_primary_locations() -> None:
    assert fd.errors_by_file(_OUT) == {"src/lib.rs": 2, "src/util.rs": 1}
    assert fd.errors_by_file("") == {}


def test_file_stats_lines_and_fns(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "m.rs"
    f.write_text("pub fn a() {}\nfn b() {}\n// not a fn\nasync fn c() {}\n")
    stats = fd.file_stats([f, tmp_path / "src" / "missing.rs"], tmp_path)
    assert stats == {"src/m.rs": {"lines": 5, "fns": 3}}  # a, b, c; missing file skipped
