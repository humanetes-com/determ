"""Pins for the per-fill class histogram — the seven integers on the iter line."""
from __future__ import annotations

from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
    class_for_kept,
    note_fill_class,
)


def test_rendered_matches_iter_line_token() -> None:
    h = FillClassHistogram(cashed=2, wrong=3, mis=1, unreachable=4,
                           dropped=0, compile=5, declined=1)
    assert h.rendered() == (
        "classes: cashed=2 wrong=3 mis=1 unreachable=4 dropped=0 compile=5 "
        "declined=1")
    assert h.total == 16
    assert h


def test_empty_histogram_is_falsey() -> None:
    assert not FillClassHistogram()
    assert FillClassHistogram().rendered().startswith("classes: cashed=0")


def test_class_for_kept_is_cashed_only_with_observed_passes() -> None:
    """Negative control: empty passing set was the else-of-U2 CASHED
    assignment (run 1064759-0f23fb68)."""
    assert class_for_kept(passed=frozenset(), unreachable=False) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    assert class_for_kept(passed={"t"}, unreachable=False) is (
        FillOutcomeClass.CASHED)
    assert class_for_kept(passed={"t"}, unreachable=True) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_class_for_kept_a_reading_that_moved_is_cashed() -> None:
    """A keep whose suite reading moved verdicts is CASHED even with an
    empty attributed set — the session keep of run 3014175-d98ed09e
    iter 3 session #1 (net +5, a target still red) and the per-fn
    object_ref keep of iter 2 (net +55, hollow leftover set). Negative
    control: the same call with the reading withheld is UNREACHABLE."""
    assert class_for_kept(
        passed=frozenset(), unreachable=False, net_delta=5) is (
        FillOutcomeClass.CASHED)
    assert class_for_kept(
        passed=frozenset(), unreachable=False, net_delta=0) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


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
