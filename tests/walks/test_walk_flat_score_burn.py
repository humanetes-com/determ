"""The walk for run `1064759-0f23fb68`, cluster `stable`, 2026-08-15/16.

The run started 20:08:29 UTC and ended 04:56 on an operator's SIGTERM after
7h48m (`deaths.jsonl`, exit=143 signal=TERM). It took seven scored sweeps: iter 1
read 79/101 and fired a crash retrigger that re-stubbed one body, iter 2 read
66/101 inside that window, and iters 3-7 read 79/101 every time. The per-binary
vectors of the six 79s are byte-identical (`unittests src/lib.rs 37/0 …
tests/test_source.rs 4/1`), so ~70 landed fills — 31, 52 and 9 of them logged at
iters 5, 6 and 7 — changed no test's verdict at all.

Nothing in the loop could end it. The plateau budget charges only on a COMPLETED
frontier traversal, and at a dispatch cap of 12 over a 59-fn frontier a traversal
is five iterations: `stall` was 1 of 3 when the run was killed. The shadow
zero-payoff detector scored every one of those sweeps as PAYING, because the
frontier moved (60 → 59 → 59 → 30 → 30) and frontier movement is one of its three
payoff axes.

What this walk asserts is that the STOP became reachable — a recorded sequence
cannot say what the run would have scored — and the negative control is the run
as it happened: with the guard off, all seven sweeps replay and no stop is taken.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain import graceful_stop
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

WALK = "1064759-0f23fb68"


@pytest.fixture(autouse=True)
def _clear_stop() -> None:
    """The stop event is process-wide: a walk that trips the guard would
    otherwise stop every test after it."""
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


def _stops(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.flat_score_stop"]


def _sweeps(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for ev, d in events if ev == "phase_c.tdd.iter"]


def test_the_walk_is_the_run_that_burned_the_night() -> None:
    """The fixture is evidence, so its shape is pinned: a distiller change that
    quietly dropped the repeated readings would leave the walk below passing
    while asserting nothing."""
    walk = load_walk(WALK)
    assert walk["entry_best"] == 79
    assert [it.get("passed") for it in walk["iterations"]] == [
        None, 66, None, 79, 79, 79, 79]
    assert {it.get("oracle") for it in walk["iterations"]} == {None, 101}
    # Both un-summarised iterations left through a crash retrigger — the reason
    # the guard records at the sweep and decides at the dispatch.
    assert [it["iter"] for it in walk["iterations"] if "retrigger" in it] == [1, 3]
    # 92 fills across the last three sweeps, and the score never moved.
    assert [it.get("fills") for it in walk["iterations"][-3:]] == [31, 52, 9]


def test_the_guard_stops_the_run_before_the_night(
        tmp_path: Path, monkeypatch) -> None:
    """The decision the fix makes reachable. The run must stop on a reading of
    79/101, and it must stop while sweeps the recorded run went on to spend are
    still ahead of it."""
    events: list[tuple[str, dict]] = []
    replay(load_walk(WALK), monkeypatch, tmp_path, events=events, gpu_guard="2")

    stops = _stops(events)
    assert len(stops) == 1, "the guard fires once and ends the run"
    assert (stops[0]["pass_count"], stops[0]["oracle_size"]) == (79, 101)
    assert stops[0]["iter"] <= 5, \
        "a stop after the recorded run's last sweep protects no GPU"
    assert graceful_stop._STOP_EVENT.is_set(), \
        "the RUN stops, not just this cluster — a plateau exit would hand " \
        "control to the merge and the merged-tree fill, which spend the GPU " \
        "this guard exists to protect"
    assert _sweeps(events)[-1]["kind"] == "flat_score_stop"


def test_the_burn_reproduces_with_the_guard_off(
        tmp_path: Path, monkeypatch) -> None:
    """The negative control, and the reason to believe the test above: the
    recorded run, replayed under the code it ran on. Every sweep is spent, the
    plateau budget never closes, and nothing stops."""
    events: list[tuple[str, dict]] = []
    replay(load_walk(WALK), monkeypatch, tmp_path, events=events, gpu_guard="0")

    assert not _stops(events)
    assert not graceful_stop._STOP_EVENT.is_set()
    sweeps = _sweeps(events)
    assert len([d for d in sweeps if d.get("pass_count") == 79]) >= 4, \
        "the walk no longer reproduces the run's repeated readings and " \
        "therefore proves nothing about the guard"
    assert max(d["iter"] for d in sweeps) >= 7, \
        "the pre-fix loop reaches the seventh sweep the run was killed on"
