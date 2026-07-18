"""Byte-equivalence pins for the regex-free tool-output helpers in
`pipeline_core.domain.test_results` (P7)."""
from __future__ import annotations

import re

from pipeline_core.domain.test_results import (
    TestOutcome,
    is_compiler_output_line,
    rustc_error_codes,
)


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
                      "first: src/context.rs:193: impl has stricter "
                      "requirements than trait")


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
