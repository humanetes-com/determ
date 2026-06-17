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


_TEST_FAILS = (
    "running 30 tests\n"
    "test framing::round_trips ... ok\n"
    "test framing::deframe_truncated ... FAILED\n"
    "test wire::checksum ... FAILED\n"
    "test result: FAILED. 28 passed; 2 failed\n"
)


def test_floor_metric_compile_phase_dominates() -> None:
    # 3 compile-error sigs, no test failures → scaled error count, test term 0.
    assert fd.floor_metric(_OUT) == 3 * 10_000


def test_floor_metric_test_phase_is_failing_count() -> None:
    # Compiles (no error sigs) → metric is the failing-test count, so a 2→1
    # reduction is a strict improvement the floor-stall can sense.
    assert fd.floor_metric(_TEST_FAILS) == 2
    assert fd.floor_metric("") == 0


def test_floor_metric_compile_outranks_any_test_count() -> None:
    # A single compile error must outrank a large failing-test count, so the
    # compile→test transition (errors→0) is always a strict improvement.
    one_err = "error[E0277]: x\n  --> src/lib.rs:1:1\n"
    assert fd.floor_metric(one_err) > fd.floor_metric(_TEST_FAILS)


def test_file_stats_lines_and_fns(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "m.rs"
    f.write_text("pub fn a() {}\nfn b() {}\n// not a fn\nasync fn c() {}\n")
    stats = fd.file_stats([f, tmp_path / "src" / "missing.rs"], tmp_path)
    assert stats == {"src/m.rs": {"lines": 5, "fns": 3}}  # a, b, c; missing file skipped
