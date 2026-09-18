"""Fill-phase clock: off at 0 never trips; an armed short bound trips once."""
from __future__ import annotations

import time

import pytest

from pipeline_core.domain.fill_phase_clock import arm, poll


def test_off_at_zero_never_trips() -> None:
    arm(0)
    assert poll() is False


def test_expired_bound_trips_once(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    events: list[str] = []
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    arm(1, log=logs.append,
        emit=lambda n, **_k: events.append(n),
        cluster="stable", iter_n=2)
    assert poll() is False
    now["t"] = 61.0
    assert poll() is True
    assert any("FILL_PHASE_STOP" in m for m in logs)
    assert events == ["phase_c.tdd.fill_phase_stop"]
    assert poll() is False
