"""Tests with the same executed slice share one session.

Run `19493-36a4e056` (2026-11-04, cluster `stable`, fill 1): the session
packer joined a test to a session only when the union fit with every file
rendered whole. Every single test of that fill already needed
`files@4,500`, so no join could pass, and the plan had 17 sessions for 18
tests, four of them pairs of world twins with identical unit sets. The
second twin's session opened on the same editable units and the same
failure, and its first change set was byte-identical to the first's.
Over every recorded `phase_c.session.plan`, 185 of 1,360 sessions had the
unit set of an earlier session of the same plan.

Claims (invented shapes, no crate names, plus the recorded plan):
- a test that fits no session at the widest step joins one when the
  joined turn 0 fits at the step both sides already need;
- a test that adds no unit joins at a narrower step as long as that step
  drops no optional section, and not when it would;
- a test that adds units and would force a narrower step than either side
  still opens its own session (splitting buys rendering there);
- on the recorded fill-1 plan, with a length model fitted to the recorded
  built sizes, no two planned sessions share a unit set.
Each carries the negative control: the pre-fix packer (no cost-free join).
"""
from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path

import pytest

from pipeline_core.domain import slice_session
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.slice_session import (
    Rendering,
    SessionSlice,
    Turn0Fit,
    plan_slices,
)

_FIXTURE = (Path(__file__).parent / "fixtures" / "session_plan"
            / "19493-36a4e056_fill1.json")

_WIDE = Rendering(36_000)
_NARROW = Rendering(4_500, tests_cut=True)
_NARROWER = Rendering(4_500, tests_cut=True, types_cap=10_000)
_DROPS = Rendering(4_500, tests_cut=True, types_cap=10_000, context_drops=1)
_LADDER = (_WIDE, _NARROW, _NARROWER, _DROPS)
_CAP = 100_000


def _units(prefix: str, n: int) -> frozenset[FnSite]:
    return frozenset(FnSite(f"src/{prefix}.rs", f"f{i}", f"impl T{i}")
                     for i in range(n))


def _fit(table: dict[tuple[int, Rendering], int]) -> Turn0Fit:
    """Length by (number of tests, step): every packing of the same test
    count renders the same at a step; the claims are about joining."""
    def length(sl: SessionSlice) -> int:
        step = Rendering(sl.file_chars, sl.tests_cut, sl.types_cap,
                         sl.context_drops)
        return table[(len(sl.tests), step)]
    return Turn0Fit(length=length, ladder=_LADDER, capacity=_CAP)


def _no_cost_free_join(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fix packer: only a join that fits at the widest step."""
    monkeypatch.setattr(slice_session, "_joins_without_cost",
                        lambda *a, **k: False)


#: One test needs the narrow step; two tests fit there too.
_FITS_AT_NARROW = {
    (1, _WIDE): 120_000, (1, _NARROW): 90_000, (1, _NARROWER): 80_000,
    (1, _DROPS): 70_000,
    (2, _WIDE): 125_000, (2, _NARROW): 95_000, (2, _NARROWER): 85_000,
    (2, _DROPS): 75_000,
}

#: Two tests do not fit at the narrow step; they fit one step narrower,
#: which still carries every optional section.
_FITS_NARROWER = {**_FITS_AT_NARROW, (2, _NARROW): 105_000}

#: Two tests fit only once an optional section is dropped.
_FITS_ONLY_DROPPING = {**_FITS_NARROWER, (2, _NARROWER): 101_000}


def _tests_per_session(plans: Collection[SessionSlice]) -> list[tuple[str, ...]]:
    return sorted(p.tests for p in plans)


def test_equal_slices_join_at_the_step_both_already_need() -> None:
    same = _units("a", 10)
    plans = plan_slices({"t_x": same, "t_y": same}, fit=_fit(_FITS_AT_NARROW))
    assert _tests_per_session(plans) == [("t_x", "t_y")]


def test_negative_control_the_widest_only_join_splits_equal_slices(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _no_cost_free_join(monkeypatch)
    same = _units("a", 10)
    plans = plan_slices({"t_x": same, "t_y": same}, fit=_fit(_FITS_AT_NARROW))
    assert _tests_per_session(plans) == [("t_x",), ("t_y",)]


def test_an_equal_slice_joins_one_step_narrower_without_dropping_a_section(
        ) -> None:
    same = _units("a", 10)
    plans = plan_slices({"t_x": same, "t_y": same}, fit=_fit(_FITS_NARROWER))
    assert _tests_per_session(plans) == [("t_x", "t_y")]
    assert plans[0].rendering.context_drops == 0


def test_an_equal_slice_does_not_join_where_a_section_would_be_dropped() -> None:
    same = _units("a", 10)
    plans = plan_slices({"t_x": same, "t_y": same},
                        fit=_fit(_FITS_ONLY_DROPPING))
    assert _tests_per_session(plans) == [("t_x",), ("t_y",)]


def test_a_test_that_adds_units_and_forces_a_narrower_step_stays_apart() -> None:
    # Overlapping, not equal: the second test brings 3 units of its own.
    base = _units("a", 10)
    more = base | _units("b", 3)
    plans = plan_slices({"t_x": base, "t_y": more}, fit=_fit(_FITS_NARROWER))
    assert _tests_per_session(plans) == [("t_x",), ("t_y",)]


# ---- the recorded plan -------------------------------------------------

def _site(label: str) -> FnSite:
    path, rest = label.split("::", 1)
    name, _, container = rest.partition(" (")
    return FnSite(path, name, container.rstrip(")"))


#: The ladder the run's plan event recorded, its first eight steps.
_REC_LADDER = (
    Rendering(36_000), Rendering(18_000), Rendering(9_000), Rendering(4_500),
    Rendering(4_500, tests_cut=True),
    Rendering(4_500, tests_cut=True, types_cap=40_704),
    Rendering(4_500, tests_cut=True, types_cap=20_352),
    Rendering(4_500, tests_cut=True, types_cap=10_176),
)
#: What one ladder step wider costs, and what one more target test adds
#: (the probe reservation is 4,500 per target test, plus its quoted fn and
#: failure text). Modelled, not recorded: the event records one built
#: length per planned session, at its own step.
_PER_STEP = 6_000
_PER_TEST = 5_500


def _recorded() -> tuple[dict[str, frozenset[FnSite]], Turn0Fit, int]:
    rec = json.loads(_FIXTURE.read_text())
    slices = {t: frozenset(_site(u) for u in us)
              for t, us in rec["slices"].items()}
    own: dict[str, tuple[int, int]] = {}
    for s in rec["sessions"]:
        step = Rendering(s["file_chars"], s["tests_cut"], s["types_cap"],
                         s["context_drops"])
        at = _REC_LADDER.index(step)
        per = s["built"] - _PER_TEST * (len(s["tests"]) - 1)
        for t in s["tests"]:
            own[t] = (per, at)

    def length(sl: SessionSlice) -> int:
        step = Rendering(sl.file_chars, sl.tests_cut, sl.types_cap,
                         sl.context_drops)
        at = _REC_LADDER.index(step)
        widest = max(own[t][0] + (own[t][1] - at) * _PER_STEP
                     for t in sl.tests)
        return widest + _PER_TEST * (len(sl.tests) - 1)

    fit = Turn0Fit(length=length, ladder=_REC_LADDER,
                   capacity=rec["capacity"])
    return slices, fit, rec["max_units"]


def _equal_unit_sets(plans: Collection[SessionSlice]) -> list[tuple[str, ...]]:
    seen: dict[tuple[FnSite, ...], tuple[str, ...]] = {}
    dup: list[tuple[str, ...]] = []
    for p in plans:
        if p.units in seen:
            dup.append(p.tests)
        else:
            seen[p.units] = p.tests
    return dup


def test_recorded_fill_plan_has_no_two_sessions_on_one_unit_set() -> None:
    slices, fit, max_units = _recorded()
    plans = plan_slices(slices, fit=fit, max_units=max_units)
    assert _equal_unit_sets(plans) == []
    # Every twin pair of the recorded plan shares a session.
    for p in plans:
        for t in p.tests:
            if "_nightly_" in t:
                assert t.replace("_nightly_", "_stable_") in p.tests, p.tests
    assert len(plans) < 17


def test_negative_control_the_recorded_plan_splits_equal_slices(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _no_cost_free_join(monkeypatch)
    slices, fit, max_units = _recorded()
    plans = plan_slices(slices, fit=fit, max_units=max_units)
    assert len(_equal_unit_sets(plans)) >= 4
