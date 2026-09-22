"""The coverage slice carries a failing test's premise as its owner set and
what ran only while the failure was reported as `rendered` — and nothing
that holds the union, so no consumer can turn the report into ownership.

Run `450977-5f48b68a` (2026-09-21): `test_downcast_low` owned 33 crate sites
on the full profile; cut at the panic handler's entry it owns 12 and the
other 21 (the chain iterator, the indenting writer, the pointer accessors)
ran only while `unwrap` formatted the error. The adapter half is exercised
by `tests/integration/test_coverage_panic_premise.py` (real cargo + gdb).
"""
from __future__ import annotations

import dataclasses

from pipeline_core.domain.coverage_slice import (
    CountedBody,
    CoverageSlice,
    slice_from_counts,
    withhold_unmeasured,
)
from pipeline_core.domain.models import FnSite

PREMISE = FnSite("src/a.rs", "lookup", "")
REPORT = FnSite("src/a.rs", "next", "impl Iterator for Links")


def test_the_slice_has_no_union_field() -> None:
    names = {f.name for f in dataclasses.fields(CoverageSlice)}
    assert "rendered" in names and "owners" in names
    assert not ({"full", "full_owners", "executed", "union"} & names)


def test_slice_from_counts_keeps_the_two_apart_and_withholding_keeps_both() -> None:
    sl = slice_from_counts(
        [CountedBody(PREMISE, 1, 1), CountedBody(REPORT, 9, 1)],
        owners={"t": frozenset({PREMISE})},
        rendered={"t": frozenset({REPORT})}, failing=("t", "u"),
        unmeasured_tests=("u",))
    assert sl.owners["t"] == {PREMISE}
    assert sl.rendered["t"] == {REPORT}
    kept, _dead = withhold_unmeasured(sl, frozenset())
    assert kept.owners["t"] == {PREMISE}
    assert kept.rendered["t"] == {REPORT}
