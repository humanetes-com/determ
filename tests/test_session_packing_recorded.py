"""Session packing on the recorded owner map of run 2965-453dd7a4, iter 1.

The plan sentence: every failing test's own coverage slice fits under
SLICE_SESSION_MAX_UNITS=32 while the union does not, so the merged group
was refused (zero sessions on every fill of that run) — and packing yields
sessions each under the cap, every test in exactly one, no test's slice
split. The fixture is the `phase_c.coverage.owner_map` event as recorded,
not a hand-made list; the planner reads only the map's shape.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.slice_session import (
    _merge_pending,
    _pending_after_filters,
    plan_slices,
    slice_plan_gaps,
)

_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "sessions"
            / "2965_owner_map_iter1.json")


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _owners() -> tuple[dict[str, frozenset[FnSite]], int]:
    data = _fixture()
    return ({t: frozenset(FnSite(f, fn, c) for f, fn, c in sites)
             for t, sites in data["owners"].items()}, int(data["max_units"]))


def test_recorded_leftover_set_every_test_fits_only_the_union_does_not() -> None:
    owners, cap = _owners()
    assert len(owners) == 32
    assert max(len(u) for u in owners.values()) <= cap
    union = frozenset().union(*owners.values())
    assert len(union) > cap
    # NEGATIVE CONTROL — the pre-packing rule: one merged group over the cap.
    pending, _, _ = _pending_after_filters(owners, frozenset())
    groups = [(n, m) for n, m in _merge_pending(pending) if n]
    assert len(groups) == 1 and len(groups[0][1]) == len(union)


def test_recorded_leftover_set_packs_into_sessions_under_the_cap() -> None:
    owners, cap = _owners()
    plans = plan_slices(owners, max_units=cap)
    assert plans, "the recorded group must no longer be refused"
    # Plan sentence on this fixture, computed not described: 13/31, 7/32,
    # 7/32, 5/32. First-fit in name order is the same partition.
    assert [(len(p.tests), len(p.units)) for p in plans] == [
        tuple(pair) for pair in _fixture()["packed_sessions"]]
    placed = sorted(t for p in plans for t in p.tests)
    assert placed == sorted(owners)
    for p in plans:
        assert len(p.units) <= cap
        for t in p.tests:
            assert owners[t] <= set(p.units)
    hubs = [u for u in frozenset().union(*owners.values())
            if all(u in units for units in owners.values())]
    assert hubs, "the recorded map has a construction spine"
    for p in plans:
        for h in hubs:
            assert h in p.units
    gaps = slice_plan_gaps(owners, max_units=cap)
    assert gaps.over_cap == 0
    assert gaps.over_union == 1
    assert gaps.fitted == len(plans)
    assert gaps.fitted_from_units == len(frozenset().union(*owners.values()))
