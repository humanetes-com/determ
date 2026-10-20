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

from pipeline_core.domain.test_results import build_phase_output

# Compile-failure markers across the toolchains we drive (rustc/cargo + a few
# JS/TS/Python ones that appear in mixed harnesses). EVERY one of them is read
# from `build_phase_output` only — the driver's own wording included. The
# driver subset (`error: could not compile`, `build failed`, …) used to be
# trusted anywhere in the capture on the premise that `--no-fail-fast` runs the
# targets that built and then reports the ones that did not; measured on cargo
# 1.95.0 / 1.97.0-nightly (2026-09-07) a compile failure in any target runs no
# binary at all, so the driver wording never legitimately follows a `running N
# tests` line. What does follow one is a test that runs cargo itself:
# trybuild's nested build of this crate relayed `error: could not compile
# `anyhow` (lib)` into a sweep whose 32 binaries all completed (run
# `2131920-86818224`, 2026-09-07), and the anywhere-trust classified that as
# BUILD — the same TASK 4 misroute `build_phase_output` was written for, one
# signal over.
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
    """Classify runner output into a :class:`FailureCategory`.

    Build signals are read from the BUILD PHASE of cargo's output only
    (:func:`build_phase_output`). A test that prints a compiler diagnostic
    into its captured stdout — a trybuild/UI harness diffing rustc's stderr
    against a fixture, a `compile_fail` doc test, a test asserting on an
    error string — is not a build failure, and reading one as such routes
    TASK 4 into its structural branch with a premise that is false on the
    face of the same output; on `anyhow` 2026-08-06 that mistake was the
    whole reason TASK 4 ran at all, on a tree with 275 of 278 tests passing.
    The driver's own `error: could not compile` is no exception: a test that
    runs cargo itself (trybuild's nested build, 2026-09-07) prints it after
    the build phase too, and a genuine compile failure never does — see
    `_BUILD_SIGNALS`.

    The rest of the classification still reads the full text: `panicked at`,
    `assertion`, `timeout` and the `running 0 tests` shape are all things a test
    legitimately says about ITSELF, and cargo does not say them.
    """
    out = test_output.lower()

    build_out = build_phase_output(test_output).lower()
    if any(s in build_out for s in _BUILD_SIGNALS):
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
