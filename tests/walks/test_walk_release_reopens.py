"""Walk: the re-admission valve must open BOTH exhaustion stores.

Distilled from run `2219473-e43ea61c` (2026-08-10, cluster `stable`): iter 3's
boundary printed `re-admitted 11 previously-exhausted fn(s)/block(s) for one
more informed attempt` and the same iteration dispatched ZERO fills — every
frontier candidate sat refill-capped in the loop-local exhausted set the disk
release never reached, the layout re-author was refused by its spent
rejection budget, the only crash was unlocalizable, and the plateau exit at
88 followed. The release was the loop's last self-recovery valve and it
opened a store nobody consults.

What a walk may assert: a DECISION became reachable — under the shipped code
a granted release whose keys name the capped unit reopens it for dispatch;
under the pre-fix information loss (a count instead of identities, modelled
here as keys naming nothing) the unit stays exhausted and the recorded
signature — a grant followed by no dispatch — reproduces. Never the score.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk


def _fill_dispatches(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.iter"
            and d.get("kind") not in ("stall_exit", "no_frontier_continue")]


def test_a_release_naming_the_capped_unit_reopens_it(
        monkeypatch, tmp_path: Path) -> None:
    walk = load_walk("2219473-e43ea61c")
    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  refill_max="2",
                  reset_returns=["src/a.rs::f::"])
    assert any("also reopened in the loop-local frontier filter" in ln
               for ln in logs), \
        "the granted release never opened the loop-local gates"


def test_the_identity_blind_release_reproduces_the_recorded_burn(
        monkeypatch, tmp_path: Path) -> None:
    """The negative control, run with the old grading injected: a release
    that carries no usable identities (the pre-fix shape — the driver got a
    COUNT) reopens nothing, and the walk ends in the recorded plateau exit
    with the unit still exhausted. A control that stops reproducing means
    the walk has drifted and proves nothing."""
    walk = load_walk("2219473-e43ea61c")
    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  refill_max="2",
                  reset_returns=["src/other.rs::nobody::"])
    assert not any("also reopened in the loop-local frontier filter" in ln
                   for ln in logs)
    assert any("pass_count plateau" in ln for ln in logs), \
        "the recorded exit is gone — the walk drifted"
