"""Byte-equivalence pins for the regex-free tool-output helpers in
`pipeline_core.domain.test_results` (P7)."""
from __future__ import annotations

import re

from pipeline_core.domain.test_results import (
    is_compiler_output_line,
    rustc_error_codes,
)


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
