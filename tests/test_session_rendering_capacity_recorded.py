"""Session planning on the recorded plan event of run 32959-4a2ff1ab, iter 5.

The plan sentence this module pins: on that run's 17:13 UTC plan the two
remaining measurable red tests — 18 and 20 executed units against a unit cap
of 32 — open as sessions, because turn 0 is sized at what the renderer
produces and a slice no packing can split is rendered narrower rather than
dropped. The negative control is the shipped-at-the-time cost model, which
charged each file the CONDENSATION THRESHOLD (`min(len(text), 36_000)`)
instead of its rendering: it refuses both, which is what the run recorded
(`opened=0` on every fill from 11:56 UTC on, `pass=149` flat for the rest of
the run).

The fixture is the event as emitted plus the per-threshold file rendering
measured on that tree by `render_slice_file` — the same function the turn-0
builder calls — not a hand-made list of sizes.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.slice_session import (
    SliceCosts,
    plan_slices,
    slice_plan_gaps,
)

_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "sessions"
            / "32959-4a2ff1ab_iter5_plan.json")



def _fit_of(costs):  # noqa: ANN001, ANN202 — a length table as the built fit
    """A `Turn0Fit` whose builder's length is this table's: the packing
    tests below are about where tests go given a length per packing and
    rendering, and the table is how they state it."""
    from pipeline_core.domain.slice_session import Rendering, Turn0Fit
    return Turn0Fit(
        length=lambda sl: costs.chars(sl.tests, sl.units,
                                      file_chars=sl.file_chars or None),
        ladder=tuple(Rendering(t) for t in costs.thresholds),
        capacity=costs.max_chars)

def _fixture() -> dict:
    data: dict = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return data


def _slices(data: dict) -> dict[str, frozenset[FnSite]]:
    return {t: frozenset(FnSite(f, fn, c) for f, fn, c in sites)
            for t, sites in data["slices"].items()}


def _costs_before(data: dict) -> SliceCosts:
    """The cost model as it shipped: one rendering, charged at the
    condensation threshold rather than measured."""
    widest = max(int(t) for t in data["file_chars_rendered"])
    return SliceCosts(
        file_chars={widest: data["file_chars_charged_before"]},
        test_chars={}, fixed_chars=data["non_file_chars"],
        max_chars=data["max_chars"])


def _costs_after(data: dict) -> SliceCosts:
    return SliceCosts(
        file_chars={int(t): files
                    for t, files in data["file_chars_rendered"].items()},
        test_chars={}, fixed_chars=data["non_file_chars"],
        max_chars=data["max_chars"])


def test_the_charged_file_term_is_not_a_size_the_renderer_ever_emits() -> None:
    """What the refusal was computed on. One file is over the threshold on
    this tree; it is charged the threshold and renders to a fraction of it."""
    data = _fixture()
    widest = max(int(t) for t in data["file_chars_rendered"])
    rendered = data["file_chars_rendered"][str(widest)]
    charged = data["file_chars_charged_before"]
    over = [f for f, n in data["file_raw_chars"].items() if n > widest]
    assert over, "the fixture must hold a file above the condensation threshold"
    for f in over:
        assert charged[f] == widest
        assert rendered[f] < charged[f] // 10
    slice_files = {u.file for u in _slices(data)["test_whitespace"]}
    assert (sum(charged[f] for f in slice_files)
            - sum(rendered[f] for f in slice_files)) > data["max_chars"] // 5


def test_before_the_recorded_refusal_reproduces() -> None:
    """NEGATIVE CONTROL. The shipped model refuses both tests, and the
    refusal is the one the run recorded."""
    data = _fixture()
    slices, cap = _slices(data), data["max_units"]
    costs = _costs_before(data)
    plans = plan_slices(slices, max_units=cap, fit=_fit_of(costs))
    assert [p.tests for p in plans] == []
    gaps = slice_plan_gaps(slices, max_units=cap, fit=_fit_of(costs))
    assert gaps.empty == data["recorded"]["empty"]
    assert (gaps.over_cap + gaps.over_chars_single
            == data["recorded"]["over_cap"])


def test_after_both_red_tests_open_as_sessions_rendered_narrower() -> None:
    """The plan's sentence, on the recorded input."""
    data = _fixture()
    slices, cap = _slices(data), data["max_units"]
    costs = _costs_after(data)
    plans = plan_slices(slices, max_units=cap, fit=_fit_of(costs))
    assert sorted(t for p in plans for t in p.tests) == [
        "test_too_long", "test_whitespace"]
    for p in plans:
        assert len(p.units) <= cap
        assert 0 < p.file_chars < costs.widest(), (
            "a slice no packing can split fits by rendering narrower")
        assert costs.chars(p.tests, p.units,
                           file_chars=p.file_chars) <= data["max_chars"]
    # Measuring the renderer is necessary and NOT sufficient: at the widest
    # threshold this slice is still over the capacity, so the re-rendering
    # is what opens the session.
    for t, units in slices.items():
        if units:
            assert costs.chars((t,), units, file_chars=costs.widest()) > \
                data["max_chars"]
    gaps = slice_plan_gaps(slices, max_units=cap, fit=_fit_of(costs))
    assert gaps.over_cap == 0 and gaps.over_chars_single == 0
    assert gaps.condensed == len(plans)
    assert "rendered below the widest threshold" in gaps.condensed_clause()


def test_the_refusal_clause_names_the_capacity_that_fired() -> None:
    """The recorded line named SLICE_SESSION_MAX_UNITS=32 for a 20-unit
    slice. A drop for the char capacity says so, and says it apart from a
    drop for the unit cap."""
    data = _fixture()
    slices, cap = _slices(data), data["max_units"]
    assert max(len(u) for u in _slices(data).values()) < cap
    gaps = slice_plan_gaps(slices, max_units=cap, fit=_fit_of(_costs_before(data)))
    clause = gaps.log_clause()
    assert "SLICE_SESSION_TURN0_CHARS" in clause
    assert "SLICE_SESSION_MAX_UNITS" not in clause
    assert "SLICE_SESSION_MAX_UNITS" in data["recorded"]["log_clause"]
