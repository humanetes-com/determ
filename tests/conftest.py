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


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op `time.sleep` for every test — retry loops run instantly.

    Behaviour-preserving: loop iteration counts are unchanged; only the wall
    clock is. No daemon `while True: sleep(...)` loops are started by the unit
    suite, so this cannot spin.
    """
    monkeypatch.setattr(time, "sleep", lambda _s: None)
