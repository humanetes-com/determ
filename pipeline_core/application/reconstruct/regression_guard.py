"""Composition glue for the global test-regression ratchet (kept out of the
pipeline.py god-file). Two config-taking functions the fill loops consume via
thin pipeline.py shims:

  - baseline(...) → pre-fill per-binary TestStateReading or None
  - gate(...)     → None (accept) | (RegressionComparison, detail) (revert)

Both self-disable (return None) when the guard env `PHASE_C_TEST_REGRESSION_GUARD`
is off or the unified-fill-loop switch is off — the env is re-read per call so
tests' `monkeypatch.setenv` still flips it.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from pipeline_core.application.reconstruct import cargo_runner
from pipeline_core.domain.test_results import RegressionComparison, TestStateReading

RunCargo = Callable[..., "tuple[int, str]"]


def _guard_on() -> bool:
    return os.getenv("PHASE_C_TEST_REGRESSION_GUARD", "1").strip() not in ("", "0", "false")


def baseline(
    run_cargo: RunCargo,
    timeout: int,
    unified_on: bool,
    test_cmd: str | None,
    cwd: Path,
    env: dict[str, str] | None,
    raw_sink: list[str] | None = None,
) -> TestStateReading | None:
    """`raw_sink`, when given, receives the raw combined capture.

    `TestStateReading` carries NAMES — `BinaryResult.failed` is a
    `frozenset[str]` — and `test_results.py` cannot import `TestFailure`
    without a cycle, since `test_report.py` imports it. So the pre-fill panic
    SITES, which the non-regression gate needs to tell two red states apart,
    are not in the reading and cannot be put there cheaply. They are in the
    text: `measure_test_state` has taken a sink since it was written, for the
    neighbouring reason ("the READING deliberately drops panic text, but crash
    attribution … is decided from exactly that text"), and this function simply
    never passed one. The caller parses the sites out with
    `test_report.parse_test_output`, leaving the reading, `BinaryResult` and
    the regression gate untouched."""
    if not (test_cmd and unified_on and _guard_on()):
        return None
    return cargo_runner.measure_test_state(
        run_cargo, test_cmd, cwd, env or {}, timeout=timeout,
        raw_sink=raw_sink)


def gate(
    run_cargo: RunCargo,
    guard: Callable[[str, str], None],
    timeout: int,
    unified_on: bool,
    test_cmd: str | None,
    cwd: Path,
    env: dict[str, str] | None,
    base: TestStateReading | None,
    *,
    stub_free: bool = False,
) -> tuple[RegressionComparison, str] | None:
    return cargo_runner.test_regression_gate(
        run_cargo, test_cmd, cwd, env, base,
        enabled=unified_on and _guard_on(), timeout=timeout, guard=guard,
        stub_free=stub_free)
