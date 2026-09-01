"""Walk: the owner-map admission is reachable once the cohort is served.

Run `9872-51b20f30` (2026-09-01, cluster `stable`) held `pass=134 fail=3`
across four sweeps with `never_served=[]` from iter 1 and ZERO
`phase_c.attribution.measured_owners` emissions: the shipped fire demanded
latest-grade unanimity over a cohort whose grade writers could never supply
it (proven-from-siblings mints, masked flats, forever-reverting units), while
the coverage owner map named a measured owner of all three failing tests
that no run had ever dispatched. The service fire keys on the premise the
futility escalation already uses — every cohort unit RETURNED from a fill —
which no grade class can hold.

Replayed on the distilled fixture of that run's sweeps. Reachability only:
the admitted owner reaches a dispatched batch; no claim about the score.
Negative control: the same replay under the unanimity premise (served =
graded) reproduces the recorded zero-emission shape — the owner reaches no
batch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.coverage_slice import CoverageSlice, bind
from pipeline_core.domain.models import ClusterFillResult, FnLocator
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The three failing tests of the 134/137 sweeps, as the harness names them.
_REDS = ("t135", "t136", "t137")
# Attributed accessors (the walk's stand-ins for the Debug-test accessor
# cohort) and the measured owner the map names on all three tests, which no
# attribution path ever produced.
_ACC = FnLocator("src/a.rs", "fmt")
_ACC2 = FnLocator("src/a.rs", "context")
_OWNER = FnLocator("src/b.rs", "owner_fmt")

_OWNERS = {t: frozenset({_ACC, _ACC2, _OWNER}) for t in _REDS}


def _coverage(_pd: Path, _cl: str, failing_tests=frozenset()):
    return CoverageSlice(owners=dict(_OWNERS), failing=frozenset(failing_tests))


def _replay(monkeypatch, tmp_path: Path):
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(
            walk, monkeypatch, tmp_path, events=events,
            frontier={_ACC: set(_REDS), _ACC2: set(_REDS)},
            fill=_fill, coverage=_coverage)
    finally:
        bind(None)
    return logs, events, batches


def test_a_served_cohort_admits_the_measured_owner_into_a_batch(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, batches = _replay(monkeypatch, tmp_path)
    admits = [d for e, d in events
              if e == "phase_c.attribution.measured_owners"]
    assert admits, "the admission must fire once the cohort has been served"
    assert "src/b.rs::owner_fmt" in admits[0]["added"]
    assert any(_OWNER in b for b in batches), (
        "the admitted owner must reach a dispatched batch")
    assert any("measured-owners:" in ln for ln in logs)
    # The accessors were served (fill-returned) before the fire — the
    # premise is service, not any grade.
    assert batches and _ACC in batches[0] and _OWNER not in batches[0]


def test_negative_control_grade_unanimity_reproduces_the_zero_emission(
        monkeypatch, tmp_path: Path) -> None:
    """The recorded shape: under the unanimity premise (only a graded unit
    counts as served) the cohort holds the fire on every sweep — zero
    admission events, the owner in no batch, exactly the live run's
    `events.jsonl`."""
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    real = _mod.escalate_to_measured_owners

    def _unanimity(**kw: Any):
        kw["served"] = frozenset(kw.get("graded") or frozenset())
        return real(**kw)

    monkeypatch.setattr(_mod, "escalate_to_measured_owners", _unanimity)
    logs, events, batches = _replay(monkeypatch, tmp_path)
    assert not any(e == "phase_c.attribution.measured_owners"
                   for e, _ in events)
    assert all(_OWNER not in b for b in batches)
