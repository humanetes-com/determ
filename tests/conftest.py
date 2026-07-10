"""Shared pytest fixtures.

All tests in this suite are OFFLINE (scripted model + cargo seams). No test
should ever incur a real wall-clock sleep: the only `time.sleep` calls reachable
from the engine are network-retry backoffs (`adapters/ask_client.py`,
`adapters/llm_transports.py`, `retry.py`) that fire when a model/transport call
hits a real (absent) server. In a scripted test that backoff is pure dead time —
a single such path once stalled a `check.sh` run for ~25 minutes. Neutralising
`time.sleep` keeps retry *counts* (and thus asserted behaviour) identical while
removing the wall-clock cost. `--timeout` in check.sh is the backstop.
"""
from __future__ import annotations

import time

import pytest

from pipeline_core.domain import graceful_stop


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op `time.sleep` for every test — retry loops run instantly.

    Behaviour-preserving: loop iteration counts are unchanged; only the wall
    clock is. No daemon `while True: sleep(...)` loops are started by the unit
    suite, so this cannot spin.
    """
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _isolate_graceful_stop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Tests that exercise the real, composition-wired fix loop (e2e tests
    calling `pipeline._phase_c_fill_impl_blocks`/etc directly, not a mocked
    deps object) go through the ACTUAL `check_graceful_stop`, which by
    default reads a hardcoded OS-level path (`/tmp/pipeline_graceful_stop`)
    — ambient state shared with whatever ELSE is running on the machine. A
    real pipeline run's own graceful-stop request (e.g. an operator
    restarting it mid-session) can leave that sentinel file briefly present
    while `check.sh` happens to run, causing every such test to
    self-terminate with `SystemExit: 0` — a real, reproduced failure with
    nothing wrong in the code under test.

    Two prior versions of this fixture tried to coordinate around the SHARED
    real path (delete-then-restore, then hide-and-restore with a
    process-unique temp name) and both failed live: `check.sh` runs pytest
    under xdist, so MULTIPLE WORKER PROCESSES run this autouse fixture
    concurrently, over and over (once per test, ~1500 times) — any scheme
    that touches the real shared path repeatedly across racing processes
    eventually loses the race and silently drops a real pending sentinel
    (reproduced live both times). The actually-robust fix is to never touch
    the shared path AT ALL: redirect `SENTINEL_PATH` to this test's own
    `tmp_path` (pytest's built-in per-test-unique temp dir) for the
    duration of the test. No other test or process can ever see or write to
    it, so there is no shared state left to race on, and the real
    production sentinel is never read, written, or renamed by any test."""
    monkeypatch.setattr(graceful_stop, "SENTINEL_PATH",
                        tmp_path / "graceful-stop-sentinel-unused-by-real-pipeline")
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_observability_sinks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Tests must not write to the PRODUCTION observability sinks
    (logs/pipeline_guards.log, logs/prompts/, logs/events.jsonl): synthetic
    600K-char guard-test prompts landed in the real guards log and prompt
    store, and were then mis-read as live-run evidence during monitoring
    (same ambient-state family as the graceful-stop sentinel incident).
    Redirect the module-level paths to pytest's per-test tmp_path; the real
    pipeline process is unaffected (it re-reads its own module state)."""
    import pipeline
    monkeypatch.setattr(pipeline, "_GUARD_LOG", str(tmp_path / "guards.log"))
    monkeypatch.setattr(pipeline, "_PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(pipeline, "_EVENTS_LOG", str(tmp_path / "events.jsonl"))
