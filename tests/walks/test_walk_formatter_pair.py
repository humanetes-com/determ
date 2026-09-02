"""Walk: the hop's pair reaches the fill engine, and the hop reads the
PREVIOUS sweep's coverage binding.

Run `358833-aa15d93c` (2026-09-01, cluster `stable`, 135/137): the paying
write was a cross-file dispatcher/implementer pair; the recorded shape was
three `src/fmt.rs` opens and two `src/error.rs` reverts with no joint
judgment possible, because nothing carried the pair from attribution to
dispatch. Replayed on that run's distilled sweeps, two reachability claims:

1. BINDING ORDER — attribution runs BEFORE the sweep's own coverage
   measurement (`bind_coverage_slice` sits after `attribute_failures` in the
   iteration), so the reading available to the hop at loop iter N is the one
   measured at iter N-1, and None at iter 1. The plan's redirect rule is
   stated against exactly that binding; this pins it instead of guessing.
2. PAIR PLUMBING — a cross-file pair on the attribution product arrives at
   the fill engine on `tdd_frontier_tests.pairs`, the seam the pair route
   dispatches from. Negative control: an attribution product without pairs
   (the recorded run's shape) hands the engine none, so no joint dispatch
   exists to reach.

No walk asserts a pass count.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain import coverage_slice as cov
from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureKind,
    FormatterPair,
)
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_REDS = ("t136", "t137")
_DISP = FnLocator("src/a.rs", "fmt")
_IMPL = FnLocator("src/b.rs", "debug")
_PAIR = FormatterPair(FnSite("src/a.rs", "fmt", "impl Debug for Error"),
                      FnSite("src/b.rs", "debug", "impl ErrorImpl"))


def _replay(monkeypatch, tmp_path: Path, *, with_pairs: bool):
    walk = load_walk("358833-aa15d93c")
    events: list[tuple[str, dict]] = []
    seen_bindings: list[CoverageSlice | None] = []
    engine_pairs: list[frozenset[FormatterPair]] = []
    measured: list[int] = []

    def _coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
        # Each sweep's measurement is identity-tagged by the iteration that
        # took it, so the binding-order assertion can say WHICH sweep's
        # reading the next attribution observed.
        measured.append(len(measured) + 1)
        return CoverageSlice(
            owners={t: frozenset({_DISP}) for t in failing_tests},
            failing=frozenset(failing_tests),
            dead_regions={FnLocator("src/x.rs", f"iter{measured[-1]}"): "x"})

    def _attribute(*_a: Any, **_k: Any) -> AttributionResult:
        seen_bindings.append(cov.current())
        return AttributionResult(
            {_DISP: set(_REDS), _IMPL: set(_REDS)},
            FailureKind.BEHAVIOR,
            {_DISP: set(_REDS), _IMPL: set(_REDS)},
            {},
            frozenset({_PAIR}) if with_pairs else frozenset())

    def _fill(**kw: Any) -> ClusterFillResult:
        ft = kw.get("tdd_frontier_tests")
        engine_pairs.append(frozenset(getattr(ft, "pairs", frozenset())))
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(walk, monkeypatch, tmp_path, events=events,
                      frontier={_DISP: set(_REDS)}, fill=_fill,
                      coverage=_coverage, attribute=_attribute)
    finally:
        cov.bind(None)
    return logs, seen_bindings, engine_pairs


def _tag(reading: CoverageSlice | None) -> str | None:
    if reading is None:
        return None
    (loc,) = reading.dead_regions
    return loc.fn_name


def test_the_hop_reads_the_previous_sweeps_binding(
        monkeypatch, tmp_path: Path) -> None:
    _logs, seen, _pairs = _replay(monkeypatch, tmp_path, with_pairs=True)
    assert len(seen) >= 3
    # Iter 1: nothing measured yet — the conservative rule's None arm.
    assert seen[0] is None
    # Iter N's attribution observes the reading measured at iter N-1.
    for n, reading in enumerate(seen[1:], start=2):
        assert _tag(reading) == f"iter{n - 1}", (n, _tag(reading))


def test_a_cross_file_pair_reaches_the_fill_engine(
        monkeypatch, tmp_path: Path) -> None:
    _logs, _seen, engine_pairs = _replay(monkeypatch, tmp_path,
                                         with_pairs=True)
    assert engine_pairs, "the replay must reach a fill"
    assert all(_PAIR in ps for ps in engine_pairs), (
        "every dispatched batch carries the hop's pair to the pair route")


def test_negative_control_no_pairs_recorded_no_pair_dispatchable(
        monkeypatch, tmp_path: Path) -> None:
    """The recorded run's shape: attribution resolved the delegation but
    carried no pair, so the engine had nothing to dispatch jointly."""
    _logs, _seen, engine_pairs = _replay(monkeypatch, tmp_path,
                                         with_pairs=False)
    assert engine_pairs
    assert all(not ps for ps in engine_pairs)
