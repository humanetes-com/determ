"""A recorded owner set stands in only where this sweep measured none.

Run `497151-a6fa205b` (2026-09-22): after `construct` kept, 91 of 150
failing tests aborted and had no owner set; the owner map now reads an
aborted run's set from the debugger's flush at its first panic. The ranking
map was the measured map with every retrospective record written over it,
so a test that already had a record kept the older set and lost the one
measured now. Negative control: the plain overwrite, which is what the
driver did.
"""
from __future__ import annotations

from pipeline_core.domain.coverage_slice import (
    records_standing_in,
    unowned_among,
)
from pipeline_core.domain.models import FnLocator, FnSite

_NOW = frozenset({FnSite("src/a.rs", "make", ""), FnSite("src/a.rs", "fails", "")})
_OLD = frozenset({FnLocator("src/a.rs", "stale")})


def test_a_measured_set_is_not_replaced_by_a_record() -> None:
    measured = {"t_aborted": _NOW}
    recorded = {"t_aborted": _OLD, "t_unowned": _OLD}
    kept = records_standing_in(recorded, measured, {"t_aborted", "t_unowned"})
    assert kept == {"t_unowned": _OLD}
    ranking = {**measured, **kept}
    assert ranking["t_aborted"] == _NOW


def test_negative_control_the_plain_overwrite_loses_the_measurement() -> None:
    measured = {"t_aborted": _NOW}
    ranking = dict(measured)
    ranking.update({"t_aborted": _OLD})
    assert ranking["t_aborted"] == _OLD


def test_only_failing_unowned_tests_are_asked_for_or_kept() -> None:
    measured = {"mod::gen_tests_2::t_owned": _NOW}
    assert unowned_among({"t_owned", "t_none"}, measured) == ["t_none"]
    assert records_standing_in({"t_none": _OLD, "t_green": _OLD}, measured,
                               {"t_none"}) == {"t_none": _OLD}
