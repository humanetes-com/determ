"""P53 opened run kept untested, on the recorded event shape.

Run `2129106-ead996ab` (2026-10-28, cluster `stable`) fill 1: three runs
opened on blockers the fill measured, and every per-fn keep of them was
`accept` / `unmeasurable` with no evidence key — the gate had been handed no
test. A keep of a planned unit with the same row, a keep of an opened unit
that carries evidence, and an opened unit's verdict after the next sweep are
not this class.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p53", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_EMPTY = {"route": "per-fn", "disposition": "accept",
          "outcome": "unmeasurable", "evidence_keys": []}


def _verdict(unit: str, **over: object) -> tuple[str, dict]:
    return ("verdict", {**_EMPTY, "unit": unit, **over})


def test_an_opened_unit_kept_with_no_evidence_is_a_hit() -> None:
    events = [
        ("opened", {"blockers": ["src/a.rs::mid", "src/a.rs::low"]}),
        _verdict("src/a.rs::mid::implShapeforAlpha"),
        _verdict("src/a.rs::low::"),
    ]
    assert census.p53_untested_keeps(events) == ["src/a.rs::mid",
                                                 "src/a.rs::low"]


def test_negative_controls_are_not_hits() -> None:
    events = [
        # a planned unit with the same row: not opened
        _verdict("src/a.rs::top::"),
        ("opened", {"blockers": ["src/a.rs::mid", "src/a.rs::low"]}),
        # an opened unit graded on evidence
        _verdict("src/a.rs::mid::", disposition="keep_deferred",
                 evidence_keys=["src/a.rs::deeper"]),
        # a name that shares a prefix with an opened unit
        _verdict("src/a.rs::middle::"),
        ("iter", {}),
        # the opened unit's verdict after the next sweep
        _verdict("src/a.rs::low::"),
    ]
    assert census.p53_untested_keeps(events) == []


def test_the_no_tests_event_on_an_opened_unit_is_a_hit() -> None:
    events = [("opened", {"blockers": ["src/a.rs::mid"]}),
              ("no_tests", {"file": "src/a.rs", "fn": "mid"})]
    assert census.p53_untested_keeps(events) == ["src/a.rs::mid"]
