"""Walk: the pair dispatch is reachable WITH its evidence, and reachable
AGAIN one pass later.

Run `537093-ffcfd139` (2026-09-01, cluster `stable`, 135/137): the pair
transaction worked and the pair PROMPT was blind — the route reimplemented a
fraction of the evidence assembly, so the one call that reached the paying
units carried none of the failing tests' source or words, and the run was
TERMed one iteration short of its own second dispatch, which would have
rebuilt the identical prompt as a silent temperature resample. Replayed on
that run's distilled sweeps, two reachability claims:

1. EVIDENCE AT THE SEAM — the frontier the fill engine receives carries, for
   each pair member, the attributed tests AND their failure text
   (`FailureFrontier.evidence`), on EVERY iteration that dispatches the
   pair. That is the seam contract the shared assembly (`block_evidence`)
   reads; the prompt-side halves — the attributed test's own file in the
   Tests section, the REFILL evidence block, and the second prompt
   differing from the first — are pinned at the route seam by
   `tests/test_pair_fill_route.py` on a real engine invocation, and on the
   LIVE records by `scripts/rebuild_pair_prompt_fixture.py`, because this
   harness deliberately models test identity (`t1..tN`).
2. SECOND-DISPATCH REACHABILITY — the seam hands the pair to the engine
   again on later iterations (the pair rides whichever half the batch
   admits, once per traversal pass), so the memory written by dispatch 1
   has a reader: the reading this plan's relaunch takes is the SECOND pair
   dispatch, and a loop that could never reach it would make Fixes 8 and 9
   unobservable by construction.

Negative control: an attribution product without pairs (the shape before the
hop recorded them) hands the engine none on any iteration. No walk asserts a
pass count.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureKind,
    FormatterPair,
)
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_REDS = ("t136", "t137")
_DISP = FnLocator("src/a.rs", "fmt")
_IMPL = FnLocator("src/b.rs", "debug")
_PAIR = FormatterPair(FnSite("src/a.rs", "fmt", "impl Debug for Error"),
                      FnSite("src/b.rs", "debug", "impl ErrorImpl"))


def _replay(monkeypatch, tmp_path: Path, *, with_pairs: bool):
    walk = load_walk("537093-ffcfd139")
    events: list[tuple[str, dict]] = []
    # Per engine call: (pairs at the seam, per-member attributed tests,
    # evidence text present for each of those tests).
    seam: list[tuple[frozenset[FormatterPair], dict, bool]] = []

    def _attribute(*_a: Any, **_k: Any) -> AttributionResult:
        return AttributionResult(
            {_DISP: set(_REDS), _IMPL: set(_REDS)},
            FailureKind.BEHAVIOR,
            {_DISP: set(_REDS), _IMPL: set(_REDS)},
            {},
            frozenset({_PAIR}) if with_pairs else frozenset())

    def _fill(**kw: Any) -> ClusterFillResult:
        ft = kw.get("tdd_frontier_tests")
        tests = {k: set(ft.tests_for(k)) for k in (_DISP, _IMPL)}
        evidenced = all(
            ft.evidence_for(t) for ts in tests.values() for t in ts)
        seam.append((frozenset(getattr(ft, "pairs", frozenset())),
                     tests, evidenced))
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  frontier={_DISP: set(_REDS)}, fill=_fill,
                  attribute=_attribute)
    return logs, seam


def test_the_pair_arrives_with_its_members_tests_and_their_evidence(
        monkeypatch, tmp_path: Path) -> None:
    _logs, seam = _replay(monkeypatch, tmp_path, with_pairs=True)
    assert seam, "the replay must reach a fill"
    for pairs, tests, evidenced in seam:
        assert _PAIR in pairs
        assert tests[_DISP] == set(_REDS) and tests[_IMPL] == set(_REDS)
        assert evidenced, ("the seam must carry each attributed test's own "
                           "failure text — the assembly's evidence input")


def test_a_second_pair_dispatch_is_reachable(
        monkeypatch, tmp_path: Path) -> None:
    """The recorded run ended one iteration short of its second dispatch;
    the replay walks past the recording's end and the seam hands the pair
    to the engine again — the reader of dispatch 1's memory exists."""
    _logs, seam = _replay(monkeypatch, tmp_path, with_pairs=True)
    assert len(seam) >= 2, "the loop must reach a second fill"
    assert all(_PAIR in pairs for pairs, _t, _e in seam)


def test_negative_control_no_pairs_recorded_none_dispatchable(
        monkeypatch, tmp_path: Path) -> None:
    _logs, seam = _replay(monkeypatch, tmp_path, with_pairs=False)
    assert seam
    assert all(not pairs for pairs, _t, _e in seam)
