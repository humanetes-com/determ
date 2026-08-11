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
# JS/TS/Python ones that appear in mixed harnesses).
_BUILD_SIGNALS = (
    "error[e", "error: could not compile", "failed to compile",
    "failed to load manifest", "failed to parse manifest", "error: aborting due to",
    "cannot find", "unresolved import", "module not found", "modulenotfounderror",
    "syntaxerror", "cannot find module", "tserror", "build failed", "compilationerror",
)

# The subset a TEST cannot produce by printing a diagnostic: these are the build
# DRIVER talking about its own failure to build, not a compiler talking about
# code. They are trusted anywhere in the output, because `--no-fail-fast` runs
# the targets that built before reporting the ones that did not — so a real
# compile failure legitimately appears after tests have run. Every other signal
# in `_BUILD_SIGNALS` is a rustc/tsc/python diagnostic, which a compile-fail
# harness quotes verbatim into its own stdout, and is trusted only inside
# `build_phase_output`.
_DRIVER_BUILD_SIGNALS = (
    "error: could not compile", "failed to compile", "failed to load manifest",
    "failed to parse manifest", "module not found", "modulenotfounderror",
    "cannot find module", "tserror", "build failed", "compilationerror",
)


class FailureCategory(StrEnum):
    """Closed set of failure kinds the fix loop routes on."""

    BUILD = "build"          # did not compile → drives the TASK 2 inner loop
    ASSERTION = "assertion"  # tests ran, an assertion failed (wrong behaviour)
    PANIC = "panic"          # runtime panic not from an assertion (logic error/unwrap)
    TIMEOUT = "timeout"      # the runner timed out
    NO_TESTS = "no_tests"    # nothing ran (harness misconfiguration)
    TEST = "test"            # catch-all for other test failures


def pass_count_is_measured(failure_type: str) -> bool:
    """Did this iteration's pass count come from tests that actually RAN?

    Three categories answer no, and they all report the same 0 a genuinely
    empty tree would: BUILD (nothing compiled), NO_TESTS (nothing ran) and
    TIMEOUT (the runner never finished). That 0 is the ABSENCE of a
    measurement, and anything that treats it as one is describing a tree it has
    never scored.

    The instance this exists for, run `3274139-76ff8d08` at 00:35:16Z on
    2026-08-11: the iteration's best-tree ratchet snapshotted 12 files at
    "0 passing" while both clusters failed to PARSE (`src/error.rs:676: error:
    expected `{`, found keyword `fn``, 70 and 59 errors), because 0 beats an
    empty slot; the flow audit then printed `C6 ratchet-sanity PASS 1 save(s)
    strictly improving`. Three hours earlier the same tree had compiled at
    197/227 and was never snapshotted. Storing the 0 also disabled the RESTORE
    arm permanently — it fires on `current < best`, and nothing is below 0 — so
    the one mechanism that could have undone the damage was locked shut by the
    damage itself.
    """
    return failure_type not in (
        FailureCategory.BUILD, FailureCategory.NO_TESTS, FailureCategory.TIMEOUT)


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

    Build signals are read from CARGO's own output only. A test that prints a
    compiler diagnostic into its captured stdout — a trybuild/UI harness diffing
    rustc's stderr against a fixture, a `compile_fail` doc test, a test
    asserting on an error string — is not a build failure, and reading one as
    such routes TASK 4 into its structural branch with a premise that is false
    on the face of the same output. See :func:`cargo_own_output`; on `anyhow`
    2026-08-06 that mistake was the whole reason TASK 4 ran at all, on a tree
    with 275 of 278 tests passing.

    The rest of the classification still reads the full text: `panicked at`,
    `assertion`, `timeout` and the `running 0 tests` shape are all things a test
    legitimately says about ITSELF, and cargo does not say them.
    """
    out = test_output.lower()

    if (any(s in build_phase_output(test_output).lower() for s in _BUILD_SIGNALS)
            or any(s in out for s in _DRIVER_BUILD_SIGNALS)):
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
