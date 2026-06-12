"""Pins for application/reconstruct/cargo_errors.py (P2 slice 4) — the
regex-free rewrites must match the regexes they replaced, boundary cases
included. The old patterns are kept HERE (test-only) as the spec."""
from __future__ import annotations

import re

from pipeline_core.application.reconstruct.cargo_errors import (
    ecodes_in_text,
    excerpt_for_code,
    focus_cargo_errors,
    has_rs_line_loc,
    is_error_header,
    strip_cargo_warnings,
)


def test_is_error_header_matches_old_regex() -> None:
    cases = ["error[E0308]: x", "error: y", "error", "errors: z", "error_code",
             " error: x", "warning: e", "error:no-space", ""]
    for c in cases:
        assert is_error_header(c) == bool(re.match(r"^error\b", c)), c


def test_has_rs_line_loc_matches_old_regex() -> None:
    cases = [" --> src/lib.rs:12:5", "foo.rs:", "x.rs:9", "lib.rs no",
             "a.rs:abc", "", "x.rs:1 y.rs:z", "y.rs:z x.rs:1"]
    for c in cases:
        assert has_rs_line_loc(c) == bool(re.search(r"\.rs:\d+", c)), c


def test_ecodes_in_text_matches_old_regex() -> None:
    texts = [
        "error[E0308]: mismatched E12 E123 E1234 E12345 xE0308 _E0277 E0277x (E0599)",
        "", "E999 E0001a", "code E0061, E0061", "E0308\nE0277", "EEEE", "E E0 E00 E000",
    ]
    for t in texts:
        assert ecodes_in_text(t) == set(re.findall(r"\bE\d{3,4}\b", t)), t


def test_excerpt_for_code_matches_old_regex() -> None:
    out = "x\nerror[E0308]: bad\nl1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n"
    m = re.search(r"error\[E0308\][^\n]*\n(?:[^\n]*\n){0,6}", out)
    assert m is not None
    assert excerpt_for_code(out, "E0308") == m.group(0).strip()[:600]
    assert excerpt_for_code(out, "E0599") is None


def test_focus_cargo_errors_tiers_target_and_cross_file() -> None:
    out = (
        "warning: unused import\n  --> src/lib.rs:1:1\n\n"
        "error[E0308]: mismatched types\n  --> src/error.rs:10:5\n   = note: x\n\n"
        "error[E0599]: no method\n  --> src/lib.rs:20:9\n\n"
    )
    res = focus_cargo_errors(out, "src/error.rs", 4000)
    assert "# Errors in target file:" in res and "E0308" in res
    assert "# Errors elsewhere in the crate" in res and "E0599" in res
    assert "warning" not in res


def test_strip_cargo_warnings_drops_blocks() -> None:
    out = "warning: w\n  --> a.rs:1:1\n\nerror: e\nkeep\n"
    # the terminating blank line is consumed with the block (verbatim behavior)
    assert strip_cargo_warnings(out) == "error: e\nkeep"
