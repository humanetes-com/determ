"""Failure classification — categorise a test/build failure from runner output
so TASK 4 can produce targeted genesis updates (BL-019).

The category used to be a bare string returned by `pipeline.classify_failure`;
:class:`FailureCategory` makes the closed set explicit and type-checked. It is a
``StrEnum`` so every existing ``== "build"`` comparison and JSON/event
serialisation keeps working unchanged.

Pure domain: substring matching on the runner's own output (not on Rust source),
stdlib only, no regex."""
from __future__ import annotations

from enum import StrEnum

# Compile-failure markers across the toolchains we drive (rustc/cargo + a few
# JS/TS/Python ones that appear in mixed harnesses).
_BUILD_SIGNALS = (
    "error[e", "error: could not compile", "failed to compile",
    "failed to load manifest", "failed to parse manifest", "error: aborting due to",
    "cannot find", "unresolved import", "module not found", "modulenotfounderror",
    "syntaxerror", "cannot find module", "tserror", "build failed", "compilationerror",
)


class FailureCategory(StrEnum):
    """Closed set of failure kinds the fix loop routes on."""

    BUILD = "build"          # did not compile → drives the TASK 2 inner loop
    ASSERTION = "assertion"  # tests ran, an assertion failed (wrong behaviour)
    PANIC = "panic"          # runtime panic not from an assertion (logic error/unwrap)
    TIMEOUT = "timeout"      # the runner timed out
    NO_TESTS = "no_tests"    # nothing ran (harness misconfiguration)
    TEST = "test"            # catch-all for other test failures


def first_diagnostic(text: str) -> str:
    """The first compiler diagnostic line in ``text``, for log lines that must
    say WHY a compile failed without dumping the whole excerpt.

    A bare "did not compile" log is unactionable: a gate that fails every
    attempt for one environmental reason (e.g. E0554 — `#![feature]` on the
    stable channel, which no authored layout can fix) reads identically to a
    model that simply wrote bad code. Returns "" when nothing looks like a
    diagnostic."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("error"):
            return stripped
    return ""


def classify_failure(test_output: str) -> FailureCategory:
    """Classify runner output into a :class:`FailureCategory`."""
    out = test_output.lower()

    if any(s in out for s in _BUILD_SIGNALS):
        return FailureCategory.BUILD
    if "timeout" in out or "timed out" in out:
        return FailureCategory.TIMEOUT
    if "0 tests" in out or "running 0 tests" in out:
        return FailureCategory.NO_TESTS

    has_panic = "panicked at" in out
    has_assertion = any(s in out for s in (
        "assertion", "assert_eq", "assert_ne", "left ==", "left: "))
    if has_panic and has_assertion:
        return FailureCategory.ASSERTION
    if has_panic:
        return FailureCategory.PANIC

    return FailureCategory.TEST
