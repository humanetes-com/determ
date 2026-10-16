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
from collections.abc import Callable, Collection
from pathlib import Path

from pipeline_core.application.reconstruct import cargo_runner
from pipeline_core.domain.models import FnLocator
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

    The reading carries the pre-fill assertion POSITIONS itself
    (`TestStateReading.positions`, filled by `test_report.reading_of_capture`
    inside `measure_test_state`), so every keep that compares an attempt to
    this baseline reads them through `test_report.judge_refill` without a
    second parse of the text."""
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
    introduced_stubs: bool = False,
    observe: Callable[[RegressionComparison], None] | None = None,
    hollow_before: int | None = None,
    written: Collection[FnLocator] = (),
) -> tuple[RegressionComparison, str] | None:
    """``observe`` receives the comparison the gate measures, on every path.

    It is the only way a score reaches the record between two sweeps: this
    module published nothing at all until 2026-08-30, so the full-suite
    reading taken on every splice was computed and dropped, and a fill phase
    of two to four hours produced no score anywhere. See
    `application/reconstruct/fill_reading.py`."""
    return cargo_runner.test_regression_gate(
        run_cargo, test_cmd, cwd, env, base,
        enabled=unified_on and _guard_on(), timeout=timeout, guard=guard,
        stub_free=stub_free, introduced_stubs=introduced_stubs,
        observe=observe, hollow_before=hollow_before, written=written)
