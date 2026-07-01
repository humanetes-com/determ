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
