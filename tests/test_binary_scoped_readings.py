"""Pins for per-binary test readings + the binary-scoped regression
comparison (the crash-regime fix behind the pass=14 plateau: whole-run counts
over crash-flickering binaries are draws, not measurements — only binaries
complete in BOTH readings testify)."""

from __future__ import annotations

from pipeline_core.domain.test_command import ensure_serial_harness
from pipeline_core.domain.test_results import (
    TestStateReading,
    compare_readings,
    parse_binary_results,
    render_regression_detail,
)

_TWO_BINARIES = """\
   Compiling anyhow v1.0.0
    Running tests/test_a.rs (target/debug/deps/test_a-123)
running 2 tests
test alpha ... ok
test beta ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
    Running tests/test_b.rs (target/debug/deps/test_b-456)
running 3 tests
test gamma ... ok
test delta ...
"""


_IGNORED_BINARY = """\
    Running tests/test_backtrace.rs (target/debug/deps/test_backtrace-be1)
running 1 test
test test_backtrace ... ignored, requires nightly
test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out
"""


def test_parse_binary_results_captures_ignored_names() -> None:
    """`ignored` is a real libtest verdict, not a missing one. Dropping the
    name leaves the oracle unable to tell a cfg-gated test from a test whose
    binary crashed, so the ignored test gets charged as a failure."""
    (b,) = parse_binary_results(_IGNORED_BINARY)
    assert b.completed and b.ignored == frozenset({"test_backtrace"})
    assert b.passed == frozenset() and b.failed == frozenset()
    # An ignored test is NOT in-flight: it must never look like a crash site.
    assert b.dangling is None


def test_parse_binary_results_sections_and_crash() -> None:
    a, b = parse_binary_results(_TWO_BINARIES)
    assert a.label == "tests/test_a.rs" and a.completed
    assert a.passed == frozenset({"alpha"}) and a.failed == frozenset({"beta"})
    assert (a.pass_count, a.fail_count) == (1, 1)
    assert a.exe_path == "target/debug/deps/test_a-123"
    # b opened but never printed its trailer → crashed; the verdict-less
    # `test delta ...` prefix names the in-flight (crashing) test.
    assert not b.completed and b.dangling == "delta"
    assert b.passed == frozenset({"gamma"}) and b.executed == 1


def test_parse_binary_results_trailer_counts_authoritative() -> None:
    # `-q`-style output: trailer counts present, no per-test lines.
    (only,) = parse_binary_results(
        "running 5 tests\ntest result: ok. 5 passed; 0 failed; 0 ignored")
    assert only.completed and only.pass_count == 5 and only.passed == frozenset()


def test_parse_binary_results_headerless_capture_tolerated() -> None:
    (only,) = parse_binary_results(
        "test b ... FAILED\ntest result: FAILED. 3 passed; 1 failed; 0 ignored")
    assert only.completed and only.pass_count == 3
    assert only.failed == frozenset({"b"})


def test_reading_aggregates() -> None:
    r = TestStateReading(parse_binary_results(_TWO_BINARIES))
    assert r.pass_count == 2  # 1 trailer + 1 observed in the crashed section
    assert r.failed_names == frozenset({"beta"})
    assert [b.label for b in r.crashed] == ["tests/test_b.rs"]


def _reading(text: str) -> TestStateReading:
    return TestStateReading(parse_binary_results(text))


_BASE = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test x ... ok
test y ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
    Running tests/b.rs (t/b-2)
running 3 tests
test p ... ok
test q ...
"""


def test_compare_excludes_binaries_crashed_at_baseline() -> None:
    # b crashed at baseline; after the change its flicker reports q FAILED —
    # that name must NOT convict the change.
    cur = _BASE.replace("test p ... ok\ntest q ...\n",
                        "test q ... FAILED\ntest r ...\n")
    cmp = compare_readings(_reading(_BASE), _reading(cur))
    assert cmp.excluded_crashed == ("tests/b.rs",)
    assert cmp.measured == ("tests/a.rs",)
    assert cmp.newly_red == frozenset()
    assert not cmp.regressed


def test_compare_flags_newly_crashed_binary() -> None:
    base = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test x ... ok
test y ... ok
test result: ok. 2 passed; 0 failed; 0 ignored
"""
    cur = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test x ... ok
test y ...
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.newly_crashed == ("tests/a.rs",)
    assert cmp.regressed
    msg = render_regression_detail(cmp)
    assert "CRASH" in msg and "tests/a.rs" in msg
    assert "completed fine before this change" in msg


def test_compare_absent_section_is_not_run_not_crash() -> None:
    # cargo fail-fast: a binary that never STARTED after the change is
    # inconclusive (not_run), never blamed as a crash.
    base = """\
    Running tests/a.rs (t/a-1)
running 1 test
test x ... FAILED
test result: FAILED. 0 passed; 1 failed; 0 ignored
    Running tests/b.rs (t/b-2)
running 1 test
test p ... ok
test result: ok. 1 passed; 0 failed; 0 ignored
"""
    cur = """\
    Running tests/a.rs (t/a-1)
running 1 test
test x ... FAILED
test result: FAILED. 0 passed; 1 failed; 0 ignored
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.not_run == ("tests/b.rs",)
    assert cmp.newly_crashed == ()
    assert not cmp.regressed


def test_compare_newly_red_within_measured_scope() -> None:
    base = """\
running 2 tests
test x ... ok
test y ... ok
test result: ok. 2 passed; 0 failed; 0 ignored
"""
    cur = """\
running 2 tests
test x ... ok
test y ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.newly_red == frozenset({"y"})
    msg = render_regression_detail(cmp)
    # The historical prefix is load-bearing (starvation classifier keys on it).
    assert msg.startswith("This compiled, but it REGRESSED previously-passing")


def test_detail_names_preexisting_crashes_as_excluded() -> None:
    cur = _BASE.replace("test x ... ok", "test x ... FAILED").replace(
        "FAILED. 1 passed", "FAILED. 0 passed")
    cmp = compare_readings(_reading(_BASE), _reading(cur))
    assert cmp.regressed  # x newly red in the measured binary
    msg = render_regression_detail(cmp)
    assert "crashed BEFORE this change" in msg and "tests/b.rs" in msg
    assert "NOT your fault" in msg


def test_ensure_serial_harness() -> None:
    assert ensure_serial_harness("cargo test -p x") == \
        "cargo test -p x -- --test-threads=1"
    assert ensure_serial_harness("cargo test -p x -- foo") == \
        "cargo test -p x -- foo --test-threads=1"
    already = "cargo test -- --test-threads=1"
    assert ensure_serial_harness(already) == already
    explicit = "cargo test -- --test-threads=4"
    assert ensure_serial_harness(explicit) == explicit
