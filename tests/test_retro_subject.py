"""A fix loop's retrospective describes the body the loop stands on.

Run `82925-203ed7b8` (2026-09-28), per-fn `context`: attempt 1 E0277, attempts
2 and 3 compiled and failed tests, attempts 4 and 5 E0277, then `KEEPING best
compiling body`. The retrospective opened "could not be made to compile after
5 attempt(s)" over attempt 5's errors, because the caller described the LAST
attempt. Invented bodies and output.
"""
from __future__ import annotations

from pipeline_core.domain.refine import (
    CompilingSalvage,
    FixProgress,
    retro_subject,
)
from pipeline_core.prompts.retrospective import build_phase_c_retro_prompt

_COMPILES_RED = FixProgress(compile_errors=0, failing_tests=2)
_RUSTC = FixProgress(compile_errors=1, failing_tests=0)


def _loop() -> CompilingSalvage:
    """The recorded shape: 1 rustc, 2 and 3 compile and fail tests, 4 and 5 rustc."""
    s = CompilingSalvage()
    s.offer("b1", _RUSTC, excerpt="error[E0277] one", attempt=1)
    s.offer("b2", _COMPILES_RED, excerpt="test probe ... FAILED (attempt 2)", attempt=2)
    s.offer("b3", _COMPILES_RED, excerpt="test probe ... FAILED (attempt 3)", attempt=3)
    s.offer("b4", _RUSTC, excerpt="error[E0277] four", attempt=4)
    s.offer("b5", _RUSTC, excerpt="error[E0277] five", attempt=5)
    return s


def test_the_subject_is_the_kept_compiling_attempt_not_the_last_one() -> None:
    """The plan's sentence. NEGATIVE CONTROL: the last attempt alone, which is
    what the caller passed before, reads as a loop that never compiled."""
    s = _loop()
    subject = retro_subject(last_body="b5", last_excerpt="error[E0277] five",
                            last_compiled=False, last_attempt=5, best=s.best)
    assert subject.compiled is True
    assert subject.body == "b2"
    assert subject.excerpt == "test probe ... FAILED (attempt 2)"
    assert subject.later_compile_failures == 3

    control = retro_subject(last_body="b5", last_excerpt="error[E0277] five",
                            last_compiled=False, last_attempt=5, best=None)
    assert (control.compiled, control.body) == (False, "b5")


def test_a_last_attempt_that_compiled_is_its_own_subject() -> None:
    s = CompilingSalvage()
    s.offer("b1", _COMPILES_RED, excerpt="one", attempt=1)
    subject = retro_subject(last_body="b2", last_excerpt="two", last_compiled=True,
                            last_attempt=2, best=s.best)
    assert (subject.body, subject.excerpt, subject.compiled,
            subject.later_compile_failures) == ("b2", "two", True, 0)


def test_a_loop_that_never_compiled_is_described_as_not_compiling() -> None:
    s = CompilingSalvage()
    s.offer("b1", _RUSTC, excerpt="e", attempt=1)
    subject = retro_subject(last_body="b1", last_excerpt="e", last_compiled=False,
                            last_attempt=1, best=s.best)
    assert subject.compiled is False and subject.later_compile_failures == 0


def _prompt(subject) -> str:  # noqa: ANN001
    return build_phase_c_retro_prompt(
        "probe", "fn probe() -> u8", "", "src/a.rs", "base", subject.body,
        subject.excerpt, 5, "", "", failed_body_cap=4000, fix_err_cap=4000,
        compiled=subject.compiled,
        later_compile_failures=subject.later_compile_failures)


def test_the_prompt_says_the_body_compiled_and_names_the_later_failures() -> None:
    s = _loop()
    text = _prompt(retro_subject(
        last_body="b5", last_excerpt="error[E0277] five", last_compiled=False,
        last_attempt=5, best=s.best))
    assert "could not be made to compile" not in text
    assert "compiled and then crashed or reddened tests after 5 attempt(s)" in text
    assert "3 later attempt(s) did not compile" in text
    assert "Best attempted body (compiled; rustc accepted it; this is the body the loop keeps)" in text
    assert "b2" in text and "error[E0277] five" not in text
    # NEGATIVE CONTROL: the pre-fix inputs on the same loop.
    old = build_phase_c_retro_prompt(
        "probe", "fn probe() -> u8", "", "src/a.rs", "base", "b5",
        "error[E0277] five", 5, "", "", failed_body_cap=4000, fix_err_cap=4000,
        compiled=False)
    assert "could not be made to compile after 5 attempt(s)" in old
    assert "error[E0277] five" in old


def test_a_compiled_last_attempt_renders_as_before() -> None:
    text = build_phase_c_retro_prompt(
        "probe", "fn probe() -> u8", "", "src/a.rs", "base", "b2", "red", 2, "",
        "", failed_body_cap=4000, fix_err_cap=4000, compiled=True)
    assert "Last attempted body (compiled; rustc accepted it):" in text
    assert "later attempt(s)" not in text
