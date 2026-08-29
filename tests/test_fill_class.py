"""Pins for the per-fill class histogram — the six integers on the iter line."""
from __future__ import annotations

from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
    note_fill_class,
)


def test_rendered_matches_iter_line_token() -> None:
    h = FillClassHistogram(cashed=2, wrong=3, mis=1, unreachable=4,
                           dropped=0, compile=5)
    assert h.rendered() == (
        "classes: cashed=2 wrong=3 mis=1 unreachable=4 dropped=0 compile=5")
    assert h.total == 15
    assert h


def test_empty_histogram_is_falsey() -> None:
    assert not FillClassHistogram()
    assert FillClassHistogram().rendered().startswith("classes: cashed=0")


def test_counter_and_note_emit() -> None:
    events: list[tuple[str, dict]] = []
    c = FillClassCounter()
    note_fill_class(
        lambda ev, data=None, **_k: events.append((ev, data or {})),
        c, FillOutcomeClass.MIS_TARGETED,
        cluster="stable", unit="src/error.rs::from", route="impl")
    assert events[0][0] == "phase_c.fill.class"
    assert events[0][1]["class"] == "mis"
    assert c.histogram().mis == 1
    assert c.histogram().combined_with(
        FillClassHistogram(wrong=2)).wrong == 2
