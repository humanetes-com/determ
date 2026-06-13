"""Monitor data layer — the I/O-and-parse half of the TUI (P6 data-vs-render split).

`monitor.py` stays the thin TUI renderer (ANSI/curses + the live loop); the functions
that READ the run's `events.jsonl` and roll it up into plain data structures live here,
so the data half is unit-testable without driving the terminal.

Facade pattern (mirrors `pipeline_core.prompts.runtime`): `configure()` sets the module
globals the readers resolve at CALL time, so `monitor.py` injects the discovered event-log
path at startup and tests can point the readers at a fixture file.
"""
from __future__ import annotations

from . import run_data
from .events_data import (
    configure,
    coverage_from_events,
    latest_compile_file_stats,
    latest_flow_audit,
    latest_test_file_stats,
    phase_c_state,
    pipeline_start_time,
    read_recent_events,
)

__all__ = [
    "configure",
    "coverage_from_events",
    "latest_compile_file_stats",
    "latest_flow_audit",
    "latest_test_file_stats",
    "phase_c_state",
    "pipeline_start_time",
    "read_recent_events",
    "run_data",
]
