"""Pins for application/reconstruct/cargo_errors.py (P2 slice 4) — the
regex-free rewrites must match the regexes they replaced, boundary cases
included. The old patterns are kept HERE (test-only) as the spec."""
from __future__ import annotations

import re

from pipeline_core.application.reconstruct.cargo_errors import (
    diagnostics_from_json,
    ecodes_in_text,
    err_sig,
    err_sig_from_diagnostics,
    err_sig_from_text,
    error_type_signals,
    excerpt_for_code,
    focus_cargo_errors,
    has_rs_line_loc,
    is_error_header,
    macro_call_sites_from_spans,
    strip_cargo_warnings,
)
from pipeline_core.domain.models import Diagnostic


def _old_err_sig_text(output: str) -> frozenset:
    return (
        frozenset(re.findall(r"error\[E\d+\]: .+", output))
        | frozenset(re.findall(r"^error: .+", output, re.MULTILINE))
    )


def test_err_sig_from_text_matches_old_regex() -> None:
    cases = [
        "error[E0308]: mismatched types",
        "error: aborting due to previous error",
        "  --> src/lib.rs:1:1\nerror[E0277]: trait not satisfied\nnote: x",
        "xerror[E0599]: no method named foo",   # marker mid-line
        "error[E0308]: ",                         # no trailing char → no match
        "error: ",                                # no trailing char → no match
        "error[EABC]: bad code",                  # non-digit code → no match
        "error[E12]: short\nerror[E0061]: a, error[E0062]: b",  # one per line, greedy
        "",
        "warning: unused",
    ]
    for c in cases:
        assert err_sig_from_text(c) == _old_err_sig_text(c), c


def test_err_sig_prefers_json_then_text_fallback() -> None:
    # No JSON present → text scan.
    assert err_sig("error[E0308]: x") == err_sig_from_text("error[E0308]: x")
    # JSON present → structured sig (code:message); text-only error ignored.
    j = '{"reason":"compiler-message","message":{"level":"error","code":{"code":"E0277"},"message":"trait","spans":[],"rendered":"r"}}'
    assert err_sig(j) == frozenset({"E0277:trait"})


def test_diagnostics_and_err_sig_from_diagnostics() -> None:
    j = (
        '{"reason":"compiler-message","message":{"level":"error",'
        '"code":{"code":"E0308"},"message":"mismatched","spans":'
        '[{"is_primary":true,"file_name":"src/e.rs","line_start":10}],"rendered":"R"}}\n'
        '{"reason":"compiler-message","message":{"level":"warning","message":"w"}}\n'
    )
    diags = diagnostics_from_json(j)
    assert diags is not None and len(diags) == 1
    d = diags[0]
    assert (d.code, d.file, d.line, d.message) == ("E0308", "src/e.rs", 10, "mismatched")
    assert err_sig_from_diagnostics(diags) == frozenset({"E0308:mismatched"})
    # No JSON at all → None (caller falls back to text).
    assert diagnostics_from_json("plain text, no json") is None
    # JSON present but no errors → empty list (a real "clean" signal).
    assert diagnostics_from_json('{"reason":"build-finished","success":true}') == []
    # Diagnostic without code → bare message sig.
    assert err_sig_from_diagnostics([Diagnostic(code="", file="", line=0,
                                                message="linker failed", rendered="")]) \
        == frozenset({"linker failed"})


def test_macro_call_sites_from_expansion_chain() -> None:
    # An error inside a macro: primary span at the macro body, the invocation
    # site carried under `expansion.span`. Nested expansions walk outward.
    spans = [{
        "is_primary": True, "file_name": "src/mac.rs", "line_start": 21,
        "expansion": {"span": {
            "file_name": "src/caller.rs", "line_start": 34,
            "expansion": {"span": {
                "file_name": "src/outer.rs", "line_start": 7}}}},
    }]
    assert macro_call_sites_from_spans(spans) == (
        ("src/caller.rs", 34), ("src/outer.rs", 7))
    # A plain span (no expansion) contributes nothing.
    assert macro_call_sites_from_spans(
        [{"is_primary": True, "file_name": "src/a.rs", "line_start": 3}]) == ()


def test_diagnostics_from_json_carries_macro_call_sites() -> None:
    j = (
        '{"reason":"compiler-message","message":{"level":"error",'
        '"code":{"code":"E0605"},"message":"non-primitive cast","spans":'
        '[{"is_primary":true,"file_name":"src/backtrace.rs","line_start":21,'
        '"expansion":{"span":{"file_name":"src/error.rs","line_start":34}}}],'
        '"rendered":"R"}}\n'
    )
    diags = diagnostics_from_json(j)
    assert diags is not None and len(diags) == 1
    d = diags[0]
    # Primary span stays the macro body; the invocation site rides alongside.
    assert (d.file, d.line) == ("src/backtrace.rs", 21)
    assert d.macro_call_sites == (("src/error.rs", 34),)
    # Wire round-trip preserves it (and stays absent when empty).
    assert Diagnostic.from_dict(d.to_dict()).macro_call_sites == (("src/error.rs", 34),)
    plain = Diagnostic(code="E0308", file="a.rs", line=1, message="m")
    assert "macro_call_sites" not in plain.to_dict()
    assert Diagnostic.from_dict(plain.to_dict()).macro_call_sites == ()


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


def test_error_type_signals_extracts_blamed_types() -> None:
    """P3.6: word-aware type-name extraction from rendered rustc errors."""
    err = (
        "error[E0308]: mismatched types\n"
        "  expected type `MyStruct`, found `OtherType`\n"
        "  no method named foo found for struct Widget\n"
    )
    sigs = dict(error_type_signals(err))
    assert "MyStruct" in sigs            # after `mismatched types` … `expected type`
    assert "Widget" in sigs              # after bare `struct`
    # each value is the char index of that name in the text
    for name, idx in error_type_signals(err):
        assert err[idx:idx + len(name)] == name


def test_error_type_signals_no_substring_false_positive() -> None:
    """P3.6 (the bug the old regex had): bare `struct` must NOT match inside the
    word `reconstruct` — word-boundary aware."""
    assert error_type_signals("could not reconstruct Foo from bytes") == []
    # but a real `struct Foo` is caught
    assert error_type_signals("a real struct Foo here")[0] == ("Foo", 14)


def test_error_type_signals_requires_capitalized_ident() -> None:
    """P3.6: lowercase / single-char names are ignored (old `[A-Z][a-zA-Z0-9]+`)."""
    assert error_type_signals("expected type foo") == []      # lowercase
    assert error_type_signals("expected type `X`") == []      # single char (<2)
    assert dict(error_type_signals("expected type `Xy`")) == {"Xy": 15}


# --- diagnostics-first focusing + ANSI-hardened text path ----------------

def _diag(code: str, file: str, rendered: str, line: int = 0):
    from pipeline_core.domain.models import Diagnostic
    return Diagnostic(code=code, file=file, line=line,
                      message=rendered.splitlines()[0],
                      rendered=rendered)


def test_focus_diagnostics_target_first_whole_blocks() -> None:
    from pipeline_core.application.reconstruct.cargo_errors import focus_diagnostics
    tgt = ("error[E0308]: mismatched types\n --> src/ptr.rs:10:5\n"
           "  expected `&str`, found `String`")
    other = "error[E0425]: cannot find value `x`\n --> src/lib.rs:3:1"
    out = focus_diagnostics([_diag("E0425", "src/lib.rs", other),
                             _diag("E0308", "src/ptr.rs", tgt)], "src/ptr.rs")
    assert out.index("E0308") < out.index("E0425")
    assert "expected `&str`, found `String`" in out      # block kept WHOLE
    assert out.startswith("# Errors in target file:")
    assert "# Errors elsewhere" in out


def test_focus_diagnostics_strips_ansi_dedups_and_skips_summaries() -> None:
    from pipeline_core.application.reconstruct.cargo_errors import focus_diagnostics
    colored = "\x1b[1m\x1b[91merror[E0133]\x1b[0m: unsafe call\n --> src/a.rs:1:1"
    out = focus_diagnostics(
        [_diag("E0133", "src/a.rs", colored),
         _diag("E0133", "src/a.rs", colored),
         _diag("", "", "error: aborting due to 2 previous errors")],
        "src/a.rs")
    assert out.count("E0133") == 1 and "\x1b" not in out
    assert "aborting" not in out


def test_focus_diagnostics_count_cap_and_empty() -> None:
    from pipeline_core.application.reconstruct.cargo_errors import focus_diagnostics
    diags = [_diag("E0308", "src/a.rs", f"error[E0308]: case {i}")
             for i in range(12)]
    out = focus_diagnostics(diags, "src/a.rs", max_target=3)
    assert out.count("error[E0308]") == 3
    assert focus_diagnostics([], "src/a.rs") == ""


def test_focus_diagnostics_line_window_moves_out_of_span_to_elsewhere() -> None:
    """Plan D4: a From block at lines 982–988 must not be told that
    impl Error's E0277 at line 145 is its own LAST_ERROR. Same-file
    diagnostics outside the window sit under a THIS-FILE header, not the
    crate-wide one (that header is ``downstream_only`` to retro).
    Line 985 stays in the target bucket."""
    from pipeline_core.application.reconstruct.cargo_errors import focus_diagnostics
    other = "error[E0277]: the size for values of type `dyn StdError` cannot be known\n --> src/error.rs:145:5"
    mine = "error[E0308]: mismatched types\n --> src/error.rs:985:9"
    out = focus_diagnostics(
        [_diag("E0277", "src/error.rs", other, line=145),
         _diag("E0308", "src/error.rs", mine, line=985)],
        "src/error.rs", line_lo=982, line_hi=988)
    assert "Errors in target file:" in out
    assert "Errors elsewhere in this file" in out
    assert "Errors elsewhere in the crate" not in out
    tgt, _, rest = out.partition("# Errors elsewhere in this file")
    assert "E0308" in tgt and "E0277" not in tgt
    assert "E0277" in rest
    # Unset window is file-scoped (byte-identical to today): both stay target.
    whole = focus_diagnostics(
        [_diag("E0277", "src/error.rs", other, line=145),
         _diag("E0308", "src/error.rs", mine, line=985)],
        "src/error.rs")
    assert "E0277" in whole.split("# Errors elsewhere")[0]


def test_focus_cargo_errors_parses_ansi_colored_headers() -> None:
    from pipeline_core.application.reconstruct.cargo_errors import focus_cargo_errors
    out_text = ("\x1b[1m\x1b[91merror[E0308]\x1b[0m: mismatched types\n"
                " --> src/ptr.rs:10:5\n"
                "  expected `&str`, found `String`\n\n")
    got = focus_cargo_errors(out_text, "src/ptr.rs", 4000)
    assert got.startswith("# Errors in target file:")   # block WAS parsed
    assert "\x1b" not in got
