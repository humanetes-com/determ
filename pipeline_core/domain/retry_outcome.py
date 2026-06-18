"""The task3 inline-retry decision logic, as explicit types (P3.D).

The retry ran in one of two implicit modes and ended on a tangle of inline
comparisons deciding whether to keep the edits, revert them, recurse, or
escalate. This module names both:

* :class:`RetryMode` — BUILD (no test reported FAILED because the tree did not
  compile → attribute via cargo diagnostics) vs TEST (≥1 test FAILED → attribute
  via the reverse-map/classifier).
* :class:`RetryVerdict` + the two pure ``*_verdict`` functions — given the
  before/after counts, what to do. Pure: no I/O, so the revert-or-keep policy is
  unit-testable on numbers alone (it was the subtlest part — R9.2: a tree that
  no longer compiles emits zero FAILED lines and must NOT read as "near success").
"""
from __future__ import annotations

from enum import StrEnum


class RetryMode(StrEnum):
    """How the retry attributes failures and re-checks progress."""

    BUILD = "build"   # tree didn't compile — cargo diagnostics drive the fix
    TEST = "test"     # ≥1 test FAILED — reverse-map/classifier drives the fix


class RetryVerdict(StrEnum):
    """What to do with the retry's result."""

    PASS = "pass"               # tests pass now — keep, report success
    REVERT = "revert"           # strictly worse — restore the snapshot
    RECURSE = "recurse"         # strict progress — re-run TASK 3 on the new tree
    ESCALATE = "escalate"       # no progress — hand to TASK 4
    KEEP_PARTIAL = "keep"       # not worse, not passing — keep partial progress


def build_verdict(*, initial_errors: int, final_errors: int,
                  has_snapshot: bool) -> RetryVerdict:
    """BUILD mode: fewer compile errors ⇒ recurse; more ⇒ revert (if we can);
    same ⇒ escalate to TASK 4."""
    if has_snapshot and final_errors > initial_errors:
        return RetryVerdict.REVERT
    if final_errors < initial_errors:
        return RetryVerdict.RECURSE
    return RetryVerdict.ESCALATE


def failing_test_verdict(*, passed: bool, initial_failures: int,
                         final_failures: int, initially_ran: bool,
                         finally_ran: bool, has_snapshot: bool) -> RetryVerdict:
    """TEST mode: pass ⇒ keep. Else revert when the retry broke the build (ran
    before, doesn't now — strictly worse than N failing tests) or increased the
    failure count; otherwise keep the partial progress."""
    if passed:
        return RetryVerdict.PASS
    broke_build = initially_ran and not finally_ran
    if has_snapshot and (broke_build or final_failures > initial_failures):
        return RetryVerdict.REVERT
    return RetryVerdict.KEEP_PARTIAL
