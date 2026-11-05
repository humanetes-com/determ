"""Walk: an aborted red test's observed owners reach the owed walk and the
session, through the one owner record (`domain/red_test.py`).

Run `1604111-bb3716ef` (2026-11-05, cluster `stable`, iter 9): the two owed
tests' binary aborted, so coverage listed them as unmeasured and
`withhold_unmeasured` removed them from the owner map the session slicer
read. The rank still admitted the six owners the aborted run had entered;
the session slice was the one attributed unit; `merge_slices` took that as
the walk's slice, so the walk owed `[[drop]]` and the admitted owners were
removed as served with nothing owing them.

Replayed on the recorded sweeps of `9872-51b20f30` (three red tests on
every sweep), with a coverage stub that has the same shape: every red test
unmeasured (its run did not complete) with a positive owner set, and an
attribution frontier naming only an accessor. Reachability only:

- the owed event's tiers hold the observed owner, and the split check
  reports no owner off the tiers;
- the session slices the fill is handed hold the owner as a site with the
  container the coverage reading carried.

Negative control: `merge_slices` without the owner view (the pre-record
composition) owes the accessor alone, and the split check names the owner
on every sweep (`HEALTH ‼ RED_TEST_SPLIT`).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct import phase_c_tdd
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.coverage_slice import CoverageSlice, bind
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

_REDS = ("t135", "t136", "t137")
_ACC = FnLocator("src/a.rs", "fmt")
_ACC_SITE = FnSite("src/a.rs", "fmt", "impl Debug for Error")
_OWNER_SITE = FnSite("src/b.rs", "drop_rest", "")
_OWNER_KEY = "src/b.rs::drop_rest"


def _coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    return CoverageSlice(
        owners={t: frozenset({_ACC_SITE, _OWNER_SITE}) for t in _REDS},
        failing=frozenset(failing_tests),
        unmeasured_tests=frozenset(_REDS))


def _attribute(*_a: Any, **_k: Any) -> AttributionResult:
    """Attribution names the accessor alone, with its container: the one
    unit the live run's session slice held."""
    front = {_ACC: set(_REDS)}
    return AttributionResult(dict(front), FailureKind.NO_FRONTIER,
                             {_ACC_SITE: set(_REDS)},
                             site_frontier={_ACC_SITE: set(_REDS)})


def _replay(monkeypatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    import tests.test_tdd_ratchet as ratchet
    base = ratchet._driver_deps

    def _with_specs(*a: Any, **k: Any) -> Any:
        return replace(base(*a, **k), behaviour_specs_for=lambda pd, cl: [])

    monkeypatch.setattr(ratchet, "_driver_deps", _with_specs)
    events: list[tuple[str, dict]] = []
    sessions: list[dict] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        sessions.append(dict(kw.get("session_slices") or {}))
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(load_walk("9872-51b20f30"), monkeypatch, tmp_path,
                      events=events, frontier={_ACC: set(_REDS)},
                      fill=_fill, coverage=_coverage, attribute=_attribute)
    finally:
        bind(None)
    return logs, events, sessions


def _owed(events: list[tuple[str, dict]]) -> list[dict]:
    return [d for e, d in events if e == "phase_c.tdd.owed"]


def test_an_aborted_tests_owners_are_on_its_tiers_and_in_its_session(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, sessions = _replay(monkeypatch, tmp_path)
    owed = _owed(events)
    assert owed, "the owed reading must run"
    for d in owed:
        for t in _REDS:
            on = {u for tier in d["tests"][t]["tiers"] for u in tier}
            assert _OWNER_KEY in on
    splits = [d for e, d in events if e == "phase_c.red_test.split"]
    assert splits and all(not d["owners_off_tiers"] for d in splits)
    assert not [ln for ln in logs if "RED_TEST_SPLIT" in ln]
    assert sessions and any(
        _OWNER_SITE in sl for s in sessions for sl in s.values())


def test_negative_control_the_session_slice_alone_owes_the_accessor(
        monkeypatch, tmp_path: Path) -> None:
    real = phase_c_tdd.merge_slices

    def _pre_record(primary, attributed, tests, *, sight, owners=None):  # type: ignore[no-untyped-def]
        return real(primary, attributed, tests, sight=sight)

    monkeypatch.setattr(phase_c_tdd, "merge_slices", _pre_record)
    # The pre-record session read the post-withhold map: no owner.
    monkeypatch.setattr(
        phase_c_tdd, "read_red_owners",
        lambda **kw: _no_session(phase_c_tdd_real_read(**kw)))
    logs, events, _sessions = _replay(monkeypatch, tmp_path)
    owed = _owed(events)
    assert owed
    for d in owed:
        for t in _REDS:
            on = {u for tier in d["tests"][t]["tiers"] for u in tier}
            assert _OWNER_KEY not in on
    splits = [d for e, d in events if e == "phase_c.red_test.split"]
    assert splits and all(_OWNER_KEY in d["owners_off_tiers"].get(t, [])
                          for d in splits for t in _REDS)
    assert [ln for ln in logs if "HEALTH ‼ RED_TEST_SPLIT" in ln]


def phase_c_tdd_real_read(**kw: Any):  # type: ignore[no-untyped-def]
    from pipeline_core.domain.red_test import read_red_owners
    return read_red_owners(**kw)


def _no_session(rec):  # type: ignore[no-untyped-def]
    """The record with its session view emptied: what the slicer had when
    it read the post-withhold owner map (an aborted test is absent)."""
    from pipeline_core.domain.red_test import RedOwners
    return RedOwners({t: replace(r, session=frozenset())
                      for t, r in rec.by_test.items()})
