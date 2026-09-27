"""Byte-equivalence pins for the regex-free tool-output helpers in
`pipeline_core.domain.test_results`."""
from __future__ import annotations

import re

from pipeline_core.domain.test_results import (
    TestOutcome,
    find_unreported_tests,
    is_compiler_output_line,
    parse_test_counts,
    rustc_error_codes,
)

# The shape `cargo test --no-fail-fast` produced on the merged `anyhow` tree at
# 2026-08-05 02:27 UTC: one binary dies after its first test and never prints a
# `test result:` line, so its 7 tests leave the numerator AND the denominator.
_CRASHED_RUN = """     Running unittests src/lib.rs (target/debug/deps/anyhow-951ab)

running 54 tests
test result: FAILED. 53 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out

     Running tests/test_downcast.rs (target/debug/deps/test_downcast-3d447)

running 7 tests
test test_as_ref ... ok
error: test failed, to rerun pass `--test test_downcast`

     Running tests/test_ensure.rs (target/debug/deps/test_ensure-0e38d)

running 18 tests
test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_a_crashed_binary_leaves_the_counts_internally_consistent():
    # This is why the defect survived: 71 + 1 is a perfectly plausible pair.
    assert parse_test_counts(_CRASHED_RUN) == (71, 1, 0)


def test_find_unreported_tests_names_the_binary_that_went_silent():
    gap = find_unreported_tests(_CRASHED_RUN)
    assert gap.announced == 79           # 54 + 7 + 18
    assert gap.reported == 72            # 53 + 1 + 18
    assert gap.missing == 7
    assert gap.silent_binaries == ("Running tests/test_downcast.rs",)
    assert gap, "a gap must be truthy — callers branch on it"


def test_a_single_test_binary_announces_in_the_singular():
    # `running 1 test`, not `1 tests`. Matching only the plural undercounted the
    # announcement by 2 on the merged `anyhow` tree (compiletest and
    # test_backtrace each hold one).
    one = ("     Running tests/compiletest.rs (target/debug/deps/c)\n"
           "\nrunning 1 test\n"
           "test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured\n")
    gap = find_unreported_tests(one)
    assert gap.announced == 1 and gap.reported == 1
    assert not gap


def test_a_clean_run_reports_no_gap():
    clean = ("     Running tests/test_ensure.rs (target/debug/deps/x)\n"
             "\nrunning 18 tests\n"
             "test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured\n")
    gap = find_unreported_tests(clean)
    assert not gap
    assert gap.missing == 0
    assert gap.announced == gap.reported == 18


def test_test_outcome_event_data_byte_identical_to_legacy_dicts():
    # Each constructor must reproduce the exact literal dict (keys + order) that
    # _task3_legacy_single historically wrote to done["data"].
    assert TestOutcome.timeout().as_event_data() == {
        "passed": False, "reason": "timeout"}
    assert TestOutcome.command_not_found().as_event_data() == {
        "passed": False, "reason": "command_not_found"}
    assert TestOutcome.from_counts(True, 7, 0).as_event_data() == {
        "passed": True, "pass_count": 7, "fail_count": 0}
    assert TestOutcome.from_counts(False, 3, 4).as_event_data() == {
        "passed": False, "pass_count": 3, "fail_count": 4}
    # key ORDER preserved (raw-JSON-safe): reason path omits counts, run path omits reason
    assert list(TestOutcome.timeout().as_event_data()) == ["passed", "reason"]
    assert list(TestOutcome.from_counts(True, 1, 2).as_event_data()) == [
        "passed", "pass_count", "fail_count"]


def test_test_outcome_is_frozen_value_object():
    import dataclasses
    o = TestOutcome.from_counts(True, 1, 0)
    assert o == TestOutcome.from_counts(True, 1, 0)   # value equality
    try:
        o.passed = False  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("TestOutcome must be frozen")


def test_rustc_error_codes_matches_regex():
    cases = [
        "error[E0277]: trait not satisfied; see E0599 and E12345",
        "E027 e0277 E12 E0277x _E0277", "no codes", "E999 E9999 E99999",
        "E0277,E0599", "(E0277)", "",
    ]
    for t in cases:
        assert rustc_error_codes(t, "E") == re.findall(r"\bE\d{3,4}\b", t), t
        low = t.lower()
        assert rustc_error_codes(low, "e") == re.findall(r"\be\d{3,4}\b", low), low


def test_is_compiler_output_line_matches_regex():
    cases = [
        "error: something", "error[E0277]: trait", "warning: unused",
        "warning[W0]: x", "error[Ex]: bad", "error", "errors: x", "fn main() {}",
        "warning[E1]: wrong code letter", "error[E]: no digits", "error[E12]:",
    ]
    for s in cases:
        expected = bool(re.match(r'^error(\[E\d+\])?:', s) or re.match(r'^warning(\[W\d+\])?:', s))
        assert is_compiler_output_line(s) == expected, s


from pipeline_core.domain.test_results import rustc_diagnostic_locations, strip_ansi  # noqa: E402


def test_strip_ansi_matches_regex():
    for t in ["\x1b[0mplain", "\x1b[31merror\x1b[0m", "no ansi", "\x1b[1;32mx",
              "\x1b[m", "\x1bnotansi", "\x1b[", "", "a\x1b[33mb\x1b[0mc"]:
        assert strip_ansi(t) == re.sub(r"\x1b\[[0-9;]*m", "", t), repr(t)


def test_rustc_diagnostic_locations_matches_regex():
    pat = re.compile(r"-->\s+([^:\s]+\.rs):(\d+):\d+")
    cases = [
        "  --> src/lib.rs:12:5\n", "--> a.rs:1:2 --> b.rs:3:4", "no location",
        "--> notrs.txt:1:2", "--> x.rs:1", "-->\tsrc/m.rs:99:1", "--> a.rs:b:c",
    ]
    for t in cases:
        assert rustc_diagnostic_locations(t) == [(m.group(1), int(m.group(2))) for m in pat.finditer(t)], t


from pipeline_core.domain.test_results import parse_error_code_note  # noqa: E402


def test_parse_error_code_note_matches_regex():
    pat = re.compile(r"^\[((?:E\d{3,4})(?:,E\d{3,4})*)\]\s*(.+)$")
    cases = ["[E0277] needs Sized", "[E0277,E0599] two codes", "[E12] short", "[E12345] long",
             "[E0277]  spaced ", "[E0277]", "[E0277] ", "[Exxx] bad", "no bracket", "[E0277,X] y", ""]
    for s in cases:
        m = pat.match(s)
        assert parse_error_code_note(s) == ((m.group(1), m.group(2).strip()) if m else None), s


from pipeline_core.domain.test_results import camelcase_type_tokens  # noqa: E402


def test_camelcase_type_tokens_matches_regex():
    pat = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:<[^<>\n]{1,60}>)?)\b")
    cases = [
        "expected Foo found Bar", "Vec<u8> and Box<dyn Error>x", "Result<T, E>",
        "Self::method", "a Vec<u8> b", "Foo<>", "T E C", "MyType_2<Inner>z",
        "no types here", "", "Foo<bar baz>q",
    ]
    for t in cases:
        assert camelcase_type_tokens(t) == [m.group(1) for m in pat.finditer(t)], t


from pipeline_core.domain.test_results import parse_phase_c_line  # noqa: E402


def test_parse_phase_c_line_matches_regex():
    pat = re.compile(r"^\s*TEST\s+(?P<test>\S+?)\s*:\s*(?P<cat>[A-Z_]+)\s*"
                     r"(?:\[fn=(?P<fn>[^\]]+)\])?\s*(?:—|-)\s*(?P<reason>.+?)\s*$")
    cases = [
        "TEST foo: FAIL — broken", "TEST bar : TIMEOUT [fn=run] - slow",
        "TEST x::y: PANIC—index out of bounds  ", "TEST a: CAT", "not a test line",
        "TEST n: LOWER - x", "  TEST z: OK [fn=f]   —   done  ", "TEST p: A—",
    ]
    for ln in cases:
        m = pat.match(ln)
        exp = (m.group("test"), m.group("cat"), m.group("fn"), m.group("reason").strip()) if m else None
        assert parse_phase_c_line(ln) == exp, ln


from pipeline_core.domain.test_results import compile_gate_reason  # noqa: E402

_E0276_OUT = """   Compiling anyhow v1.0.0
error[E0276]: impl has stricter requirements than trait
  --> src/context.rs:193:5
error[E0599]: no method named `provide_ref`
  --> src/context.rs:201:9
error: could not compile `anyhow` (lib)
"""


def test_compile_gate_reason_names_the_gate():
    # a 1-error compile gate must not read as a harness crash.
    reason = compile_gate_reason("nightly", _E0276_OUT)
    assert reason == ("cluster 'nightly': lib failed to compile — 2 error(s), "
                      "first: src/context.rs:193: error[E0276]: impl has "
                      "stricter requirements than trait")


def test_compile_gate_reason_none_without_diagnostics():
    # No parseable rustc error (crash/timeout) → keep the generic wording.
    assert compile_gate_reason("stable", "TIMEOUT: exceeded 600s") is None
    assert compile_gate_reason("stable", "") is None


from pipeline_core.domain.test_results import summarize_test_binaries  # noqa: E402

_MULTI_BIN = """   Compiling anyhow
     Running unittests src/lib.rs (target/debug/deps/anyhow-ab12)

running 12 tests
test a ... ok
test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured

     Running tests/test_boxed.rs (target/debug/deps/test_boxed-cd34)

running 8 tests
test result: FAILED. 6 passed; 2 failed; 0 ignored

   Doc-tests anyhow

running 3 tests
test result: ok. 3 passed; 0 failed; 0 ignored
"""


def test_summarize_test_binaries_per_target_breakdown():
    # one (label, passed, failed) per cargo test target, in order.
    assert summarize_test_binaries(_MULTI_BIN) == [
        ("unittests src/lib.rs", 12, 0),
        ("tests/test_boxed.rs", 6, 2),
        ("Doc-tests anyhow", 3, 0),
    ]


_SPLIT_STREAM = """\
running 12 tests
test a ... ok
test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured

running 8 tests
test result: FAILED. 6 passed; 2 failed; 0 ignored

--- stderr ---
   Compiling anyhow
     Running unittests src/lib.rs (target/debug/deps/anyhow-ab12)
     Running tests/test_boxed.rs (target/debug/deps/test_boxed-cd34)
"""


def test_summarize_test_binaries_split_stream_layout_keeps_labels():
    # Captured cargo output puts the stderr `Running` lines AFTER every
    # stdout section — labels must still bind by section index, not `?`.
    assert summarize_test_binaries(_SPLIT_STREAM) == [
        ("unittests src/lib.rs", 12, 0),
        ("tests/test_boxed.rs", 6, 2),
    ]


_CRASHED_SECTION = """\
running 12 tests
test a ... ok
test result: ok. 12 passed; 0 failed; 0 ignored

running 8 tests
test b ... ok

running 3 tests
test result: ok. 3 passed; 0 failed; 0 ignored

--- stderr ---
     Running unittests src/lib.rs (target/x)
     Running tests/test_boxed.rs (target/y)
   Doc-tests anyhow
"""


def test_summarize_test_binaries_crashed_section_consumes_its_label():
    # The middle binary crashed (no `test result:` line) — it must still
    # consume its label so the next section doesn't get misattributed.
    assert summarize_test_binaries(_CRASHED_SECTION) == [
        ("unittests src/lib.rs", 12, 0),
        ("Doc-tests anyhow", 3, 0),
    ]


def test_summarize_test_binaries_empty_and_no_regex():
    assert summarize_test_binaries("") == []
    assert summarize_test_binaries("error[E0308]: mismatched") == []
    # negative/odd tokens must not crash the pure-string parser
    one = "test result: FAILED. 0 passed; 5 failed; 0 ignored"
    assert summarize_test_binaries(one) == [("?", 0, 5)]


# --- cargo target attribution on a failed build -------------------------------
_BUILD_FAIL = """\
error[E0277]: `NonNull<anyhow::error::ErrorImpl>` cannot be sent between threads safely
   --> tests/test_autotrait.rs:5:5
error: could not compile `anyhow` (test "test_autotrait") due to 2 previous errors
error: could not compile `anyhow` (test "test_repr") due to 1 previous error
"""


def test_compile_failed_targets_dedups_and_keeps_order():
    from pipeline_core.domain.test_results import compile_failed_targets
    assert compile_failed_targets(_BUILD_FAIL) == (
        'test "test_autotrait"', 'test "test_repr"')
    # same target twice collapses; order is first-seen
    twice = _BUILD_FAIL + '\nerror: could not compile `anyhow` (test "test_autotrait") due to 1\n'
    assert compile_failed_targets(twice) == (
        'test "test_autotrait"', 'test "test_repr"')


def test_compile_failed_targets_reads_through_ansi_colour():
    from pipeline_core.domain.test_results import compile_failed_targets
    coloured = '\x1b[1m\x1b[31merror\x1b[0m: could not compile `anyhow` (lib) due to 3\n'
    assert compile_failed_targets(coloured) == ("lib",)


def test_lib_compile_clean_separates_shape_failures_from_body_failures():
    from pipeline_core.domain.test_results import lib_compile_clean
    # Only integration binaries failed → the lib built → no body can be at fault.
    assert lib_compile_clean(_BUILD_FAIL)
    # The lib itself failed → a filled body is a live explanation.
    assert not lib_compile_clean(
        'error: could not compile `anyhow` (lib) due to 3 previous errors\n')
    # `lib test` is the lib compiled with cfg(test) — still the lib.
    assert not lib_compile_clean(
        'error: could not compile `anyhow` (lib test) due to 1 previous error\n')
    # A build where the lib ALSO failed is not clean even if binaries failed too.
    assert not lib_compile_clean(
        _BUILD_FAIL + 'error: could not compile `anyhow` (lib) due to 1\n')


def test_lib_compile_clean_is_false_without_evidence():
    from pipeline_core.domain.test_results import lib_compile_clean
    # Absence of a parseable target is UNKNOWN, never "the lib is fine" — a
    # caller gated on this must not act on a link error or an ICE.
    assert not lib_compile_clean("")
    assert not lib_compile_clean("error: linking with `cc` failed: exit status: 1")
    assert not lib_compile_clean("test result: FAILED. 3 passed; 2 failed")


_TRYBUILD_AFTER_OPEN = (
    "tests/fixtures/crash/asan_trybuild_stderr_after_open_section_12616.txt")


def _read_trybuild_fixture() -> str:
    from pathlib import Path
    return (Path(__file__).parent.parent / _TRYBUILD_AFTER_OPEN).read_text(
        encoding="utf-8", errors="replace")


def test_a_foreign_coloured_test_line_does_not_replace_the_in_flight_test():
    # Recorded sanitized sweep of run `12616-8345ae1d` (2026-09-27): the last
    # stdout section printed its in-flight test and aborted; trybuild's
    # ANSI-coloured progress lines arrive later on stderr.
    from pipeline_core.domain.test_results import parse_binary_results
    by_label = {b.label: b for b in parse_binary_results(_read_trybuild_fixture())}
    crashed = by_label["tests/wrapper_gen_6.rs"]
    assert not crashed.completed
    assert crashed.dangling == "test_boxed_error_debug_via_as_ref"
    assert not any("\x1b" in n for b in by_label.values()
                   for n in b.passed | b.failed)


def test_the_foreign_line_reading_is_the_phantom_name(monkeypatch):
    # Negative control: the same capture, read without the name rule, names
    # the crashed binary's in-flight test after a trybuild UI case.
    from pipeline_core.domain import test_results
    monkeypatch.setattr(test_results, "_has_control", lambda _t: False)
    by_label = {b.label: b for b in
                test_results.parse_binary_results(_read_trybuild_fixture())}
    assert "temporary-value.rs" in (by_label["tests/wrapper_gen_6.rs"].dangling
                                    or "")


def test_a_coloured_libtest_verdict_still_parses():
    # libtest colours the verdict slot, never the name: that line stays a
    # test line with its name intact.
    from pipeline_core.domain.test_results import parse_binary_results
    out = ("running 1 test\n"
           "test foo::bar ... \x1b[32mok\x1b[0m\n")
    (b,) = parse_binary_results(out)
    assert b.dangling == "foo::bar"
