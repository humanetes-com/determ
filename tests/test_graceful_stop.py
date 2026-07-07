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
