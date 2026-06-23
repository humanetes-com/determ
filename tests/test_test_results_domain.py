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
