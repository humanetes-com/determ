"""P18 — locator-collapsed session slice. The detector reads the no-slice
log and the locator-only expansion line.

Plan sentence: a run whose session was dropped because eligible-as-source
exploded past the cap is a census row; an empty leftover (`ui` only) is
not. Negative control: the old collapsed `hollow, empty, or over` line
(run 110993-6efa23be) still matches; a split log that names only empty
does not.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p18_collapsed_three_way_line_is_the_positive_instance() -> None:
    """Run 110993-6efa23be printed one clause for three causes."""
    line = (
        "  [phase-c/session] no slice planned "
        "(3 failing test(s) — hollow, empty, or over SLICE_SESSION_MAX_UNITS=32)"
    )
    hits = census.detect_p18(_run(line), [])
    assert len(hits) == 1
    assert "hollow, empty, or over" in hits[0]["evidence"]
    assert census.RE_SESSION_OVER_CAP.search(line) is not None


def test_p18_split_over_cap_line_is_the_class() -> None:
    line = (
        "  [phase-c/session] no slice planned "
        "(3 failing test(s) — 1 empty; 1 over SLICE_SESSION_MAX_UNITS=32 "
        "(38 unit(s)))"
    )
    hits = census.detect_p18(_run(line), [])
    assert len(hits) == 1
    assert "over SLICE_SESSION_MAX_UNITS=32" in hits[0]["evidence"]
    assert "38 unit(s)" in hits[0]["evidence"]


def test_p18_empty_only_is_not_the_class() -> None:
    """Negative control: ui with 0 units is a leftover, not a collapse."""
    line = (
        "  [phase-c/session] no slice planned "
        "(1 failing test(s) — 1 empty)"
    )
    assert census.detect_p18(_run(line), []) == []
    assert census.RE_SESSION_OVER_CAP.search(line) is None


def test_p18_locator_only_expansion_is_the_plan_path_collapse() -> None:
    line = (
        "  [phase-c/session] locator-only w.rs::fmt expanded to 4 tree site(s)"
    )
    hits = census.detect_p18(_run(line), [])
    assert len(hits) == 1
    assert "w.rs::fmt" in hits[0]["evidence"]
    assert "4 tree site" in hits[0]["evidence"]


def test_p18_session_opening_is_the_reaction() -> None:
    assert any(m == "session-opened" for _, m in census.REACTIONS["P18"])
    assert "P18" in census.DETECTORS and "P18" in census.CLASS_NAMES
