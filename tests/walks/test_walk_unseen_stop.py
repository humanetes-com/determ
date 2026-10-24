"""Walk: the run stops, naming the gap, at the first sweep on which every
owed red test is unseen.

Run `1947461-b4eb65c3` (2026-10-24, cluster `stable` judged in two worlds)
climbed 100 → 362 of 364 and sat at 362 for iters 13 and 14. From iter 13 the
owed red test was built only under a cfg the owner probe's world did not set:
the owner map answered nothing for it, attribution fell through to the
recently-filled ring, the iteration line printed `unowned=0`, and fills 13
(88 minutes) and 14 aimed every run off the failing path.

Replayed on the distilled sweeps of that run (`tests/fixtures/walks/
1947461-b4eb65c3.json`). The coverage stub is the recorded shape: every
failing test owned while the climb lasts, the plateau's red tests answered
by no probed binary (`unclaimed`; the recorded owner map was the home world
alone, so the walk cannot show the world name, and the reason is the one a
one-world reading gives). The frontier is a guess attributed to the plateau
tests. Reachability only: the stop fires at the first 362 sweep, before that
iteration's dispatch, and names each test and its reason. Negative control:
the same replay with no coverage port (sight not taken, the pre-change
reading) dispatches the guess at that sweep.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.coverage_slice import CoverageSlice, bind
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The two red tests of the 362/364 sweeps, as the harness names them.
_PLATEAU = ("t363", "t364")
_GUESS = FnLocator("src/a.rs", "recently_filled")
_OWNER = FnSite("src/a.rs", "owner", "")


def _coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k: Any) -> CoverageSlice:
    # The harness runs with an empty cluster env, so the home world is the
    # unnamed one ("") and the sweep's pairs carry no world prefix.
    names = frozenset(failing_tests)
    if names <= set(_PLATEAU):
        return CoverageSlice(failing=names, worlds=frozenset({""}))
    return CoverageSlice(owners={t: frozenset({_OWNER}) for t in names},
                         failing=names, worlds=frozenset({""}))


def _replay(monkeypatch, tmp_path: Path, *, coverage) -> tuple[  # type: ignore[no-untyped-def]
        list[str], list[tuple[str, dict]], list[int]]:
    walk = load_walk("1947461-b4eb65c3")
    events: list[tuple[str, dict]] = []
    filled_at: list[int] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        filled_at.append(int(kw.get("fill_iteration") or 0))
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(walk, monkeypatch, tmp_path, events=events,
                      max_stalls="0", unseen_stop="1",
                      frontier={_GUESS: set(_PLATEAU)}, fill=_fill,
                      coverage=coverage)
    finally:
        bind(None)
    return logs, events, filled_at


def _first_plateau_iter(events: list[tuple[str, dict]]) -> int:
    return min(int(d["iter"]) for ev, d in events
               if ev == "phase_c.tdd.iter" and d.get("pass_count") == 362)


def test_the_stop_fires_at_the_first_all_unseen_sweep_before_dispatch(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, filled_at = _replay(monkeypatch, tmp_path, coverage=_coverage)
    stops = [d for ev, d in events if ev == "phase_c.tdd.unseen_stop"]
    assert len(stops) == 1, [ln for ln in logs if "UNSEEN" in ln]
    stop = stops[0]
    at = _first_plateau_iter(events)
    assert stop["iter"] == at and stop["pass_count"] == 362
    assert set(stop["tests"]) == set(_PLATEAU)
    assert {v["reason"] for v in stop["tests"].values()} == {"unclaimed"}
    assert "src/a.rs::recently_filled" in stop["shadow"]
    assert at not in filled_at and filled_at, filled_at
    line = next(ln for ln in logs if "HEALTH ‼ UNSEEN_STOP" in ln)
    assert "t363 (unclaimed" in line and "TDD_UNSEEN_STOP=0" in line


def test_negative_control_without_the_sight_the_guess_is_dispatched(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, filled_at = _replay(monkeypatch, tmp_path, coverage=None)
    assert not [d for ev, d in events if ev == "phase_c.tdd.unseen_stop"]
    assert _first_plateau_iter(events) in filled_at


def test_a_seen_test_is_not_dispatched_on_the_guess(
        monkeypatch, tmp_path: Path) -> None:
    """The same sweeps, with the coverage stub naming an owner for every
    failing test. The plateau tests are seen, so the stop does not fire,
    and the iteration that sits at 362 dispatches the owner and not the
    guess."""
    def _seen(_pd: Path, _cl: str, failing_tests=frozenset(), **_k: Any,
              ) -> CoverageSlice:
        names = frozenset(failing_tests)
        return CoverageSlice(
            owners={t: frozenset({_OWNER}) for t in names},
            failing=names, worlds=frozenset({""}))

    dispatched: list[tuple[int, tuple]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        it = int(kw.get("fill_iteration") or 0)
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        dispatched.append((it, keys))
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    walk = load_walk("1947461-b4eb65c3")
    events: list[tuple[str, dict]] = []
    try:
        replay(walk, monkeypatch, tmp_path, events=events,
               max_stalls="0", unseen_stop="1",
               frontier={_GUESS: set(_PLATEAU)}, fill=_fill,
               coverage=_seen)
    finally:
        bind(None)
    assert not [d for ev, d in events if ev == "phase_c.tdd.unseen_stop"]
    at = _first_plateau_iter(events)
    at_keys = [keys for it, keys in dispatched if it == at]
    assert at_keys, dispatched
    owner = FnLocator(_OWNER.file, _OWNER.fn_name)
    assert all(_GUESS not in keys and owner in keys for keys in at_keys)
