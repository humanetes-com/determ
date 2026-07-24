"""Pins for the stall-triggered auto-operator state machine (domain/auto_operator).
The rules that make an unattended stop-fix-restart safe live here: fire once per
stall episode, never overlap a running pass, re-arm only when the stall clears
or the mode is disabled."""
from __future__ import annotations

from pipeline_core.domain.auto_operator import AutoOperator


class _Proc:
    """Opaque process handle: poll() is None until `done` is set."""
    def __init__(self) -> None:
        self.done: int | None = None

    def poll(self) -> int | None:
        return self.done


def test_fires_once_per_episode_and_re_arms_on_clear() -> None:
    procs: list[_Proc] = []

    def launch() -> _Proc:
        p = _Proc()
        procs.append(p)
        return p

    a = AutoOperator(trigger=3)

    a.tick(True, 1, launch)            # below trigger — no fire
    assert len(procs) == 0
    a.tick(True, 3, launch)            # reaches trigger — fire once
    assert len(procs) == 1 and a.running
    a.tick(True, 3, launch)            # still stalled + running — no re-fire
    assert len(procs) == 1

    procs[0].done = 0                  # pass finished, but stall still high
    a.tick(True, 3, launch)            # reaped, yet not re-armed → no fire
    assert len(procs) == 1 and not a.running

    a.tick(True, 0, launch)            # stall cleared → re-arm
    assert a.armed
    a.tick(True, 3, launch)            # next episode fires again
    assert len(procs) == 2


def test_disabled_never_fires() -> None:
    fired: list[int] = []

    def launch() -> object:
        fired.append(1)
        return object()

    a = AutoOperator(trigger=3)
    a.tick(False, 99, launch)
    assert fired == [] and not a.running


def test_disabling_mid_episode_re_arms() -> None:
    """Turning the mode off (sentinel removed) must not leave `armed=False`
    stuck — the next stall episode after re-enabling should fire again."""
    fired: list[int] = []

    def launch() -> _Proc:
        fired.append(1)
        return _Proc()

    a = AutoOperator(trigger=3)
    a.tick(True, 3, launch)
    assert len(fired) == 1 and not a.armed
    a.tick(False, 3, launch)          # disabled mid-episode
    assert a.armed
