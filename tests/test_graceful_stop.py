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
    sentinel = graceful_stop.sentinel_path()   # under this test's control dir
    sentinel.touch()
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(lambda _m: None, "worker-a")
    assert not sentinel.exists()


def test_second_caller_still_exits_after_sentinel_is_gone(monkeypatch, tmp_path):
    # The bug this guards: sentinel already consumed by another "thread" —
    # the OLD code's file-existence check alone would silently no-op here.
    sentinel = graceful_stop.sentinel_path()   # under this test's control dir
    sentinel.touch()
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(lambda _m: None, "worker-a")
    assert not sentinel.exists()
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(
            lambda _m: None, "worker-b, or the main thread post-join")


def test_fill_phase_clock_expiry_stops_via_check_graceful_stop(monkeypatch):
    """An armed short bound trips FILL_PHASE_STOP then the same
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


# --- a stop inside a unit's fix loop ends the unit through its route's exit --
# Run `3495074-71c576c7` (2026-10-20): an impl attempt for `Drop for Error`
# read `pass 94 -> 223 (net +129) … no regression`, was banked as salvage, and
# the stop raised `SystemExit` at the next attempt's boundary. The block's
# exit, which writes the salvage back, never ran; the tree went back to 94.

def _loop_with_stop_during_attempt_one():
    from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
    from pipeline_core.domain.refine import CompilingSalvage, FixProgress

    salvage = CompilingSalvage()
    written: list[str] = []

    def attempt(n: int) -> FixOutcome:
        # Compiles, 32 attributed tests still red: banked, not accepted.
        salvage.offer("fn drop(&mut self) { /* body */ }", FixProgress(0, 32))
        graceful_stop.sentinel_path().touch()       # the operator's stop
        return FixOutcome("failed", err_sig=frozenset({f"t{n}"}),
                          metric=FixProgress(0, 32))

    accepted, attempts = drive_fix_loop(
        attempt, hard_cap=5, max_stalls=3, stop_context="per-impl fix",
        check_stop=lambda ctx: graceful_stop.check_graceful_stop(
            lambda _m: None, ctx))
    # The route's exit: write back the banked image.
    if not accepted and salvage.best is not None:
        written.append(salvage.best.body)
    return accepted, attempts, written


def test_a_stop_inside_a_fix_loop_lets_the_unit_write_back_its_banked_image():
    accepted, attempts, written = _loop_with_stop_during_attempt_one()
    assert (accepted, attempts) == (False, 1)
    assert written == ["fn drop(&mut self) { /* body */ }"]
    # The stop is still taken, at the next boundary between units.
    with pytest.raises(SystemExit):
        graceful_stop.check_graceful_stop(lambda _m: None, "between units")


def test_negative_control_the_old_boundary_raises_and_the_exit_never_runs(
        monkeypatch):
    from pipeline_core.domain import unit_clock

    # Pre-fix `ended`: a stop was not a reason, so the attempt boundary's
    # `check_stop` raised from inside the loop.
    monkeypatch.setattr(unit_clock, "ended", lambda tests=None: "")
    with pytest.raises(SystemExit):
        _loop_with_stop_during_attempt_one()


def test_the_unit_clock_names_a_requested_stop():
    from pipeline_core.domain import unit_clock

    unit_clock.clear()
    assert unit_clock.ended() == ""
    graceful_stop.request_graceful_stop()
    assert unit_clock.ended() == "stop"
    line, event, _data = unit_clock.end_note("stop", attempts=2)
    assert line.startswith("STOP_REQUESTED") and event == "phase_c.unit.stop_requested"
