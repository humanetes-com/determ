"""Unit tests for the failure-classification domain (P6 prerequisite slice) —
moved from pipeline.classify_failure, now returning a typed FailureCategory."""
from __future__ import annotations

from pipeline_core.domain.failure_classification import (
    FailureCategory,
    classify_failure,
    first_diagnostic,
)


def test_build_wins_over_everything() -> None:
    assert classify_failure("error[E0308]: mismatched types") is FailureCategory.BUILD
    assert classify_failure("error: could not compile `foo`") is FailureCategory.BUILD
    # build precedence: even with a panic present, a compile error means BUILD.
    assert classify_failure("error[E0425]\npanicked at lib.rs") is FailureCategory.BUILD


def test_timeout_and_no_tests() -> None:
    assert classify_failure("test timed out after 60s") is FailureCategory.TIMEOUT
    assert classify_failure("running 0 tests") is FailureCategory.NO_TESTS


def test_assertion_vs_panic() -> None:
    assert classify_failure("thread panicked at 'assertion failed: left == right'") \
        is FailureCategory.ASSERTION
    assert classify_failure("thread 'main' panicked at unwrap on None") is FailureCategory.PANIC


def test_catch_all_test() -> None:
    assert classify_failure("test result: FAILED. 1 failed") is FailureCategory.TEST
    assert classify_failure("some unrecognised output") is FailureCategory.TEST


def test_is_a_str_for_back_compat() -> None:
    # StrEnum: equality with the legacy string values still holds.
    assert classify_failure("error[E0308]") == "build"
    assert FailureCategory.NO_TESTS == "no_tests"


def test_first_diagnostic_names_the_error() -> None:
    out = ("   Compiling x v0.1.0\n"
           "warning: unused import\n"
           "error[E0554]: `#![feature]` may not be used on the stable release channel\n"
           "  --> src/lib.rs:210:42\n"
           "error: aborting due to 1 previous error\n")
    # The FIRST diagnostic, not the last — rustc puts the primary cause first,
    # and "aborting due to" names no cause at all.
    assert first_diagnostic(out) == (
        "error[E0554]: `#![feature]` may not be used on the stable release channel")


def test_first_diagnostic_empty_when_no_diagnostic() -> None:
    assert first_diagnostic("") == ""
    assert first_diagnostic("   Compiling x\nwarning: unused\n") == ""
