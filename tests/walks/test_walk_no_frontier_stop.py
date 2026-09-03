"""Walk: an empty frontier under `TDD_MAX_STALLS=0` must reach a NAMED stop,
never the 10 000-iteration backstop — and must spend the only move left first.

Distilled from run `1246060-2339497b` (2026-08-21, cluster `stable`): every
stop knob at 0, two complete traversals of the 39-unit frontier read 83/22/105
at both pass-complete sweeps, and from iteration 9 every unit sat in the
loop-local exhausted set. The branch for "nothing dispatchable" logged `no
actionable frontier this iter but stall=N/0 — continuing` and looped straight
back to a cached `cargo test` at ~2 iterations a second — 133 such iterations
are in the fixture, 3 797 were in the log by 04:51 UTC — until the backstop
closed the cluster EXHAUSTED at iteration 10 000 and the flow advanced to
`nightly` on a tree nobody had chosen to stop on.

What a walk may assert: decisions. Under the shipped code the loop (1) spends
the no-other-move re-admission on the held unit and DISPATCHES it again — a
unit held as exhausted became reachable — and (2) once that budget is spent,
stops under its own name within a handful of iterations. Under the pre-fix
grading (the stop disabled) the recorded spin reproduces to the backstop.
Never the score.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_NO_FRONTIER = ("no_frontier_continue", "no_frontier_readmit",
                "no_frontier_stop")


def _kinds(events: list[tuple[str, dict]]) -> list[str]:
    return [str(d.get("kind")) for ev, d in events if ev == "phase_c.tdd.iter"]


def _run(monkeypatch, tmp_path: Path, *, stop_after: str,
         events: list[tuple[str, dict]]) -> list[str]:
    walk = load_walk("1246060-2339497b")
    # `refill_max=2` is what empties the one-fn frontier: after two behavioural
    # re-fills the unit is refill-capped into the loop-local exhausted set, the
    # D3 filter holds it, and `eligible` is empty — the recorded state. The
    # stop under test and the control's bound are ARGUMENTS, not ambient env:
    # the harness pins every stop knob, so nothing here depends on the
    # operator's untracked `.env`. `backstop=60` bounds the control — the
    # recorded backstop is 10 000 iterations.
    return replay(walk, monkeypatch, tmp_path, events=events,
                  max_stalls="0", refill_max="2",
                  no_frontier_stop=stop_after, backstop="60", reset_cap="2")


def test_the_held_unit_is_re_admitted_and_dispatched_then_the_loop_stops(
        monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    logs = _run(monkeypatch, tmp_path, stop_after="2", events=events)
    kinds = _kinds(events)
    # (1) the only move left was taken: the held unit was re-admitted...
    readmits = [i for i, k in enumerate(kinds) if k == "no_frontier_readmit"]
    assert readmits, f"no re-admission under no other move: {kinds}"
    assert any("no actionable frontier and no other move — re-admitting"
               in ln and "src/a.rs::f" in ln for ln in logs)
    # ...and DISPATCHED again: a dispatching iteration follows the first
    # re-admission (a decision that was unreachable in the recorded run).
    assert any(k not in _NO_FRONTIER for k in kinds[readmits[0] + 1:]), kinds
    # (2) the budget is bounded, and once spent the loop stops under a name.
    assert kinds.count("no_frontier_readmit") == 2
    assert kinds[-1] == "no_frontier_stop"
    assert any("NO_FRONTIER_STOP" in ln for ln in logs)
    stop = [d for ev, d in events if ev == "phase_c.tdd.no_frontier_stop"]
    assert stop and stop[0]["readmissions"] == 2 and stop[0]["cap"] == 2
    # Long before the backstop, and never through it.
    assert len(kinds) < 60
    assert not any("backstop 60 reached" in ln for ln in logs)


def test_the_recorded_spin_reproduces_with_the_stop_disabled(
        monkeypatch, tmp_path: Path) -> None:
    """The negative control, run with the pre-fix grading injected
    (`TDD_NO_FRONTIER_STOP=0`): with the re-admission budget spent the loop
    takes `no_frontier_continue` on every iteration until the backstop closes
    the cluster — the recorded signature. A control that stops reproducing
    means the walk has drifted and proves nothing."""
    events: list[tuple[str, dict]] = []
    logs = _run(monkeypatch, tmp_path, stop_after="0", events=events)
    kinds = _kinds(events)
    assert "no_frontier_stop" not in kinds
    assert any("backstop 60 reached" in ln for ln in logs), kinds[-5:]
    # The spin: the tail of the replay is nothing but continues.
    assert kinds[-20:] == ["no_frontier_continue"] * 20
