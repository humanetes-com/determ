"""`check_graceful_stop` under PIPELINE_PARALLELISM>1.

`sys.exit()` only unwinds the CALLING thread. Before this fix, a worker
thread detecting the stop sentinel would unlink it, log, and quietly die —
every OTHER worker (and the main thread) never saw the file gone and kept
running, sometimes for hours, since nothing else recorded the request.
`_STOP_EVENT` makes the request visible process-wide so a SECOND caller
(simulating another thread, or the main thread's post-join check) still
exits even though the sentinel file itself is already gone.
"""
from __future__ import annotations

import pytest

from pipeline_core.domain import graceful_stop


@pytest.fixture(autouse=True)
def _reset_event():
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


def test_no_sentinel_no_event_is_a_noop():
    logged: list[str] = []
    graceful_stop.check_graceful_stop(logged.append, "idle")  # must not raise
    assert logged == []


def test_first_caller_unlinks_sentinel_and_exits(monkeypatch, tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.touch()
    monkeypatch.setattr(graceful_stop, "SENTINEL_PATH", sentinel)
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(lambda _m: None, "worker-a")
    assert not sentinel.exists()


def test_second_caller_still_exits_after_sentinel_is_gone(monkeypatch, tmp_path):
    # The bug this guards: sentinel already consumed by another "thread" —
    # the OLD code's file-existence check alone would silently no-op here.
    sentinel = tmp_path / "sentinel"
    sentinel.touch()
    monkeypatch.setattr(graceful_stop, "SENTINEL_PATH", sentinel)
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(lambda _m: None, "worker-a")
    assert not sentinel.exists()
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(
            lambda _m: None, "worker-b, or the main thread post-join")


def test_fill_phase_clock_expiry_stops_via_check_graceful_stop(monkeypatch):
    """T3: an armed short bound trips FILL_PHASE_STOP then the same
    process-wide exit a sentinel stop takes. Off at 0 never emits."""
    import time

    from pipeline_core.domain import fill_phase_clock

    logs: list[str] = []
    events: list[str] = []
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    fill_phase_clock.arm(0, log=logs.append)
    graceful_stop.check_graceful_stop(logs.append, "idle")
    assert logs == []
    fill_phase_clock.arm(
        1, log=logs.append,
        emit=lambda n, **_k: events.append(n),
        cluster="stable", iter_n=2)
    now["t"] = 61.0
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(logs.append, "fill")
    assert any("FILL_PHASE_STOP" in m for m in logs)
    assert events == ["phase_c.tdd.fill_phase_stop"]
    assert any("fill-phase bound" in m for m in logs)
    assert not any("stop sentinel" in m for m in logs)
    # Once: a second poll does not re-emit; the event still exits.
    n = len(logs)
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(logs.append, "fill again")
    assert not any("FILL_PHASE_STOP" in m for m in logs[n:])
