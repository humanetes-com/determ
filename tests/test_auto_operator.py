"""Pins for the monitor's [a] auto-operator state machine (domain/auto_operator).
The rules that make an unattended stop-fix-restart safe live here: fire once per
stall episode, never overlap a running pass, re-arm only when the stall clears."""
from __future__ import annotations

from pipeline_core.domain.auto_operator import AutoOperator, active_stall


class _Proc:
    """Opaque process handle: poll() is None until `done` is set."""
    def __init__(self) -> None:
        self.done: int | None = None

    def poll(self) -> int | None:
        return self.done


def test_active_stall_reads_active_cluster_only() -> None:
    st = {"active": True, "cluster": "nightly",
          "tdd_by_cluster": {"stable": {"stall": 9}, "nightly": {"stall": 2}}}
    assert active_stall(st) == 2
    # Inactive / malformed / missing → 0 (never a spurious trigger).
    assert active_stall({"active": False, "cluster": "nightly"}) == 0
    assert active_stall({"active": True, "cluster": "x", "tdd_by_cluster": {}}) == 0
    assert active_stall({"active": True, "cluster": "x",
                         "tdd_by_cluster": {"x": {"stall": None}}}) == 0
    assert active_stall("not a dict") == 0


def test_fires_once_per_episode_and_re_arms_on_clear() -> None:
    procs: list[_Proc] = []

    def launch() -> _Proc:
        p = _Proc()
        procs.append(p)
        return p

    a = AutoOperator(trigger=3, enabled=True)

    a.tick(1, launch)                 # below trigger — no fire
    assert len(procs) == 0
    a.tick(3, launch)                 # reaches trigger — fire once
    assert len(procs) == 1 and a.running
    a.tick(3, launch)                 # still stalled + running — no re-fire
    assert len(procs) == 1

    procs[0].done = 0                 # pass finished, but stall still high
    a.tick(3, launch)                 # reaped, yet not re-armed → no fire
    assert len(procs) == 1 and not a.running

    a.tick(0, launch)                 # stall cleared → re-arm
    assert a.armed
    a.tick(3, launch)                 # next episode fires again
    assert len(procs) == 2


def test_disabled_never_fires() -> None:
    fired: list[int] = []
    a = AutoOperator(trigger=3, enabled=False)
    a.tick(99, lambda: fired.append(1) or object())
    assert fired == [] and not a.running


def test_toggle_and_label() -> None:
    a = AutoOperator(trigger=3)
    assert a.label() == ("dim", "[a]uto")
    a.toggle()
    assert a.enabled and a.armed and a.label() == ("cyan", "[a]uto ON ≥3")
    a.proc = _Proc()
    assert a.label() == ("green", "[a]uto ⚙running")
    a.toggle()
    assert not a.enabled
