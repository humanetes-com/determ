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
from pipeline_core.application.reconstruct.fill_ground_lock import (
    check_measurement,
    note_premise_reading,
)
from pipeline_core.domain.fill_ground import MeasureRole
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
    *,
    role: MeasureRole,
) -> TestStateReading | None:
    """`raw_sink`, when given, receives the raw combined capture.

    `role` is required: the same measurement is the PREMISE a run is judged
    against and the JUDGMENT of a write it made (`domain/fill_ground`). While
    a fill's ground is bound the call must hold the tree's splice lock, and
    a premise is checked against the accepted bytes
    (`fill_ground_lock.check_measurement`), before the guard's on/off switch
    so the check holds whether or not the reading is taken.

    The reading carries the pre-fill assertion POSITIONS itself
    (`TestStateReading.positions`, filled by `test_report.reading_of_capture`
    inside `measure_test_state`), so every keep that compares an attempt to
    this baseline reads them through `test_report.judge_refill` without a
    second parse of the text."""
    check_measurement(cwd, role)
    if not (test_cmd and unified_on and _guard_on()):
        return None
    reading = cargo_runner.measure_test_state(
        run_cargo, test_cmd, cwd, env or {}, timeout=timeout,
        raw_sink=raw_sink)
    if role is MeasureRole.PREMISE and reading is not None:
        note_premise_reading(cwd, reading.pass_count)
    return reading


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
    `application/reconstruct/fill_reading.py`.

    The gate is the JUDGMENT of a write the caller made inside its hold of
    the tree's splice lock, after a premise read in the same hold; while a
    fill's ground is bound, a gate outside that hold would measure another
    worker's in-flight splice, and is refused like a premise outside it
    (`fill_ground_lock.check_measurement`). A call with no baseline judges
    nothing and is not checked."""
    if base is not None:
        check_measurement(cwd, MeasureRole.JUDGMENT)
    return cargo_runner.test_regression_gate(
        run_cargo, test_cmd, cwd, env, base,
        enabled=unified_on and _guard_on(), timeout=timeout, guard=guard,
        stub_free=stub_free, introduced_stubs=introduced_stubs,
        observe=observe, hollow_before=hollow_before, written=written)
