"""A compile retry carries the fix the compiler offered.

Fixture: the `<COMPILE_ERRORS>` block of retry prompt `e58cedda…` (run
`486447-5207101c`, 2026-10-07, file route `src/ensure.rs`): one error,
`cannot find macro format`, with rustc's three `use` alternatives. The
prompt led it with "do not resubmit the same approach" and the reply dropped
the operands it had been formatting.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.compiler_help import compiler_fixes
from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix

_DIAG = (Path(__file__).parent / "fixtures" / "compile"
         / "format_macro_help_e58cedda.txt").read_text(encoding="utf-8")


def test_the_three_alternatives_are_read() -> None:
    helped = compiler_fixes(_DIAG)
    assert helped.errors == 1
    assert helped.covers_every_error
    assert [f.choices for f in helped.fixes] == [(
        "use crate::__private::format;", "use std::format;",
        "use alloc::format;")]


def test_the_lead_keeps_the_approach_and_names_every_choice() -> None:
    lead = last_error_prefix(_DIAG, LastErrorKind.COMPILE)
    assert "same approach" not in lead
    assert "Keep that body and apply the fix" in lead
    for choice in ("use crate::__private::format;", "use std::format;",
                   "use alloc::format;"):
        assert f"`{choice}`" in lead


def test_an_error_with_no_help_keeps_todays_sentence() -> None:
    """Negative control: no `help:` carrying code, today's wording."""
    diag = ("error[E0308]: mismatched types\n  --> src/a.rs:3:5\n   |\n"
            "3 |     x\n   |     ^ expected `u8`, found `u16`\n")
    assert compiler_fixes(diag).fixes == ()
    assert last_error_prefix(diag, LastErrorKind.COMPILE).endswith(
        "do not resubmit the same approach:\n")


def test_a_help_with_no_code_is_not_a_fix() -> None:
    diag = ("error[E0277]: the trait bound `A: B` is not satisfied\n"
            "  --> src/a.rs:3:5\n   |\n"
            "help: the trait `B` is not implemented for `A`\n")
    assert compiler_fixes(diag).fixes == ()


def test_partial_help_names_every_choice_and_keeps_the_sentence_where_none() -> None:
    """Two errors, one carrying the three `use` lines and
    one `E0308` with no help. The old lead said "where the compiler's
    `help:` names a fix, apply that fix" and named nothing. Each error is
    now its own sentence: the helped one names every choice and keeps the
    body for that error; the unhelped one keeps `same approach`."""
    diag = (_DIAG + "\nerror[E0308]: mismatched types\n  --> src/a.rs:3:5\n"
            "   |\n3 |     x + y\n   |     ^ expected `u8`\n")
    helped = compiler_fixes(diag)
    assert helped.errors == 2 and helped.errors_with_fix == 1
    assert [bool(e.fixes) for e in helped.per_error] == [True, False]
    lead = last_error_prefix(diag, LastErrorKind.COMPILE)
    first, _, second = lead.partition("error[E0308]")
    for choice in ("use crate::__private::format;", "use std::format;",
                   "use alloc::format;"):
        assert f"`{choice}`" in first
    assert "keep that body for this error" in first
    assert "same approach" not in first
    assert "src/a.rs:3:5" in second
    assert "do not resubmit the same approach for this error" in second
    # The one-error diagnostic keeps the full lead and no `same approach`.
    assert "same approach" not in last_error_prefix(_DIAG, LastErrorKind.COMPILE)


def test_an_inline_suggestion_is_read() -> None:
    diag = ("error: cannot find macro `prntln` in this scope\n"
            " --> src/a.rs:2:5\n  |\n2 |     prntln!(\"x\");\n"
            "  |     ^^^^^^ help: a macro with a similar name exists: `println`\n")
    # rustc puts a short suggestion on the caret line; it is a fix too.
    assert compiler_fixes(diag).fixes[0].choices == ("println",)
    diag2 = ("error[E0425]: cannot find value `y` in this scope\n"
             "help: a local variable with a similar name exists: `x`\n")
    assert compiler_fixes(diag2).fixes[0].choices == ("x",)
