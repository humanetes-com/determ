"""Pins for per-binary test readings + the binary-scoped regression
comparison (the crash-regime fix behind the pass=14 plateau: whole-run counts
over crash-flickering binaries are draws, not measurements — only binaries
complete in BOTH readings testify)."""

from __future__ import annotations

from pipeline_core.domain.test_command import ensure_serial_harness
from pipeline_core.domain.test_results import (
    RegressionComparison,
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


def test_parse_binary_results_captured_stdout_opens_no_phantom_section() -> None:
    # trybuild prints its own `test <ui file> ... ok` progress lines, and cargo
    # replays them from the failed binary's captured stdout AFTER the trailer.
    # ANSI-coloured, so the verdict slot holds foreign text and the line reads
    # as an in-flight test. It must not open a section: doing so invented a
    # second, never-completed "crashed binary" in every anyhow sweep.
    out = ("running 1 test\n"
           "test compiles ... FAILED\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored\n"
           "failures:\n"
           "---- compiles stdout ----\n"
           "test \x1b[1mtests/ui/temporary-value.rs\x1b[0m ... \x1b[32mok\n")
    (only,) = parse_binary_results(out)
    assert only.completed and only.failed == frozenset({"compiles"})


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


# --- a regression verdict names its own class ------------------------------- #

def _cmp(**kw) -> RegressionComparison:
    base = {
        "newly_red": frozenset(), "newly_crashed": (), "excluded_crashed": (),
        "not_run": (), "measured": ("tests/a.rs",), "base_pass": 5,
        "cur_pass": 5, "base_executed": 5, "cur_executed": 5,
    }
    return RegressionComparison(**{**base, **kw})


def test_summary_and_signature_name_the_regression_class() -> None:
    # Each class gets its OWN label and its OWN stall key — collapsing them
    # onto "(pass-count drop)" mislabels a crash and makes two different
    # failures read as one repeated one.
    crash1 = _cmp(newly_crashed=("tests/a.rs",), crash_lost_pass=1)
    assert crash1.summary == "binary crashed: ['tests/a.rs'] (net -1 test(s))"
    assert crash1.signature == frozenset({"__binary_crash__:tests/a.rs"})

    crash2 = _cmp(newly_crashed=("tests/a.rs", "tests/b.rs"))
    assert crash2.summary == (
        "binaries crashed: ['tests/a.rs', 'tests/b.rs'] (net +0 test(s))")
    assert crash2.signature == frozenset(
        {"__binary_crash__:tests/a.rs", "__binary_crash__:tests/b.rs"})

    red = _cmp(newly_red=frozenset({"t_b", "t_a"}))
    assert red.summary == "newly red: ['t_a', 't_b']"
    assert red.signature == frozenset({"t_a", "t_b"})

    dropped = _cmp(cur_pass=3)
    assert dropped.summary == "pass count 5→3"
    assert dropped.signature == frozenset({"__pass_count_drop__"})

    fewer = _cmp(cur_executed=4)
    assert fewer.summary == "executed count 5→4"
    assert fewer.signature == frozenset({"__executed_count_drop__"})

    clean = _cmp()
    assert not clean.regressed and clean.summary == "" and clean.signature == frozenset()


def test_crash_outranks_a_count_drop_in_the_label() -> None:
    # A crashed binary takes its passes with it, so both signals fire at once;
    # the crash is the actionable one and must win the label.
    both = _cmp(newly_crashed=("tests/a.rs",), cur_pass=0, cur_executed=0)
    assert both.summary.startswith("binary crashed")
    assert both.signature == frozenset({"__binary_crash__:tests/a.rs"})


def test_a_strict_net_gain_outweighs_a_crashed_binary() -> None:
    """The live deadlock this rule exists for: `error.rs::construct` is the
    funnel every `construct_from_*` needs, and filling it crashed one binary
    that was passing a SINGLE test. Under an absolute veto that one test
    outranked every test the fill unblocked, so the only fn gating the crate
    was reverted on every attempt of every run."""
    cmp = _cmp(newly_crashed=("tests/test_context.rs",), crash_lost_pass=1,
               base_pass=36, cur_pass=80)
    assert cmp.net_pass_delta == 43
    assert not cmp.regressed


def test_a_crash_that_costs_more_than_it_buys_still_convicts() -> None:
    cmp = _cmp(newly_crashed=("tests/a.rs",), crash_lost_pass=9,
               base_pass=36, cur_pass=40)
    assert cmp.net_pass_delta == -5
    assert cmp.regressed and cmp.summary.endswith("(net -5 test(s))")


def test_newly_red_is_forgiven_only_by_a_strict_net_gain() -> None:
    # +2 green for 1 red is progress; a lateral swap (net 0) is not, and keeps
    # the old absolute rule so the loop cannot thrash on equal-score churn.
    gain = _cmp(newly_red=frozenset({"t_a"}), base_pass=5, cur_pass=7)
    assert not gain.regressed
    swap = _cmp(newly_red=frozenset({"t_a"}), base_pass=5, cur_pass=5)
    assert swap.regressed


def test_vanished_tests_are_never_traded_away() -> None:
    # Tests that stop EXECUTING inside a still-completing binary are oracle
    # corruption, not a behavioural cost — no pass-count gain buys them.
    cmp = _cmp(base_pass=5, cur_pass=40, base_executed=5, cur_executed=4)
    assert cmp.net_pass_delta == 35 and cmp.regressed


def test_compare_readings_charges_the_crash_what_it_was_passing() -> None:
    base = """\
    Running tests/a.rs (t/a-1)
running 3 tests
test x ... ok
test y ... ok
test z ... FAILED
test result: FAILED. 2 passed; 1 failed; 0 ignored
"""
    cur = """\
    Running tests/a.rs (t/a-1)
running 3 tests
test x ...
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.newly_crashed == ("tests/a.rs",)
    assert cmp.crash_lost_pass == 2
    # The crashed binary contributes to neither measured total — its cost is
    # carried by crash_lost_pass alone, so it is counted exactly once.
    assert (cmp.base_pass, cmp.cur_pass) == (0, 0)
    assert cmp.net_pass_delta == -2 and cmp.regressed


def test_all_red_crash_costs_nothing_without_a_special_case() -> None:
    # crashed_all_red is now an emergent property of the accounting: a binary
    # passing zero tests at baseline subtracts zero, so it cannot convict.
    cmp = _cmp(crashed_all_red=("tests/a.rs",), crash_lost_pass=0)
    assert cmp.net_pass_delta == 0 and not cmp.regressed


def test_compare_all_red_binary_crash_is_not_a_regression() -> None:
    # Baseline completed with ZERO passes (every test already failing);
    # after the fill it aborts (e.g. a filled constructor lets tests reach a
    # still-stubbed Drop). Nothing that worked was lost — reported in
    # crashed_all_red, never a conviction.
    base = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test x ... FAILED
test y ... FAILED
test result: FAILED. 0 passed; 2 failed; 0 ignored
"""
    cur = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test x ...
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.crashed_all_red == ("tests/a.rs",)
    assert cmp.newly_crashed == ()
    assert not cmp.regressed


def test_compare_mixed_crash_convicts_only_binaries_with_baseline_passes() -> None:
    base = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test p ... ok
test q ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
    Running tests/b.rs (t/b-2)
running 1 test
test r ... FAILED
test result: FAILED. 0 passed; 1 failed; 0 ignored
"""
    cur = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test p ...
    Running tests/b.rs (t/b-2)
running 1 test
test r ...
"""
    cmp = compare_readings(_reading(base), _reading(cur))
    assert cmp.newly_crashed == ("tests/a.rs",)   # held a baseline pass
    assert cmp.crashed_all_red == ("tests/b.rs",)  # was all-red
    assert cmp.regressed
    msg = render_regression_detail(cmp)
    assert "tests/b.rs" in msg and "ALREADY failing" in msg


def test_reading_passed_names_completed_binaries_only() -> None:
    text = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test p ... ok
test q ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
    Running tests/b.rs (t/b-2)
running 2 tests
test r ... ok
test s ...
"""
    r = _reading(text)
    # b crashed: its partial `ok` for r is a nondeterministic prefix, not a
    # "was passing" fact.
    assert r.passed_names == frozenset({"p"})


def test_reading_ignored_names_collected() -> None:
    text = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test n ... ignored, requires nightly
test p ... ok
test result: ok. 1 passed; 0 failed; 1 ignored
"""
    assert _reading(text).ignored_names == frozenset({"n"})

def test_reading_judged_names_completed_binaries_only() -> None:
    # The comparison base for "this body destroyed a verdict": both outcomes
    # count (a test that failed before and is unjudged now also lost its
    # verdict), but a crashed binary's partial output is not a verdict.
    text = """\
    Running tests/a.rs (t/a-1)
running 2 tests
test p ... ok
test q ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
    Running tests/b.rs (t/b-2)
running 2 tests
test r ... ok
test s ...
"""
    r = _reading(text)
    assert r.judged_names == frozenset({"p", "q"})


def test_a_section_with_no_label_is_named_by_its_position() -> None:
    # `Running <desc>` labels are stderr lines and the `running N tests`
    # sections are stdout; a live episode had EVERY label render as `?`. The
    # old fallback then applied an un-auditable exclusion to the u2 regression
    # verdict on its behalf — `binaries ['?', 'tests/test_downcast.rs'] were
    # already crashed at baseline`, every few minutes for a whole run.
    out = ("running 1 test\ntest a ... ok\n"
           "test result: ok. 1 passed; 0 failed; 0 ignored\n"
           "running 1 test\ntest b ... ")     # second section: no label, crashed
    res = parse_binary_results(out)
    assert [b.label for b in res] == ["unnamed section #1", "unnamed section #2"]
    assert res[1].completed is False and res[1].dangling == "b"


def test_an_unnamed_target_falls_back_to_its_executable() -> None:
    out = ("Running  (target/debug/deps/mystery-9f2)\n"   # empty description
           "running 1 test\ntest a ... ok\n"
           "test result: ok. 1 passed; 0 failed; 0 ignored\n")
    (b,) = parse_binary_results(out)
    assert b.label == "target/debug/deps/mystery-9f2"
    assert b.exe_path == "target/debug/deps/mystery-9f2"


_SHOULD_PANIC_BINARY = """\
     Running unittests src/lib.rs (target/debug/deps/anyhow-f8e3aaf01e9ae568)
running 3 tests
test error::gen_tests_4::test_no_backtrace_fallback - should panic ... ok
test error::gen_tests_4::test_wrong_message - should panic ... FAILED
test src/lib.rs - Error (line 12) ... ok
test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_parse_binary_results_strips_should_panic_qualifier() -> None:
    """libtest prints a `#[should_panic]` verdict as `test NAME - should panic
    ... ok`. The qualifier is not part of the name the oracle expects, so kept
    in the parsed name it turns a passing test into a vanished one — the
    `test_no_backtrace_fallback` "crash" logged on every `stable` sweep from
    2026-08-12 to 2026-08-20 was this line, passing. Doc-test names keep
    their ` - `."""
    (b,) = parse_binary_results(_SHOULD_PANIC_BINARY)
    assert b.completed
    assert b.passed == frozenset({
        "error::gen_tests_4::test_no_backtrace_fallback",
        "src/lib.rs - Error (line 12)",
    })
    assert b.failed == frozenset({"error::gen_tests_4::test_wrong_message"})
    assert b.dangling is None


def test_should_panic_in_flight_line_is_the_bare_name() -> None:
    """A binary that dies while a should_panic test is running leaves
    `test NAME - should panic ...` with no verdict; the crash site must be
    named the way attribution names it."""
    from pipeline_core.domain.test_results import strip_libtest_qualifier
    assert strip_libtest_qualifier("a::b - should panic") == "a::b"
    assert strip_libtest_qualifier("src/lib.rs - Error (line 12)") == (
        "src/lib.rs - Error (line 12)")
    assert strip_libtest_qualifier("plain") == "plain"
