"""P21 — over-union session refusal while never-served units wait.

Plan sentence: a split `no slice planned … N over SLICE_SESSION_MAX_UNITS=`
line with no locator-only expansion in the same fill, on a sweep whose
service-order line counted never-served units, is a census row; the same
refusal after a locator-only expansion in that fill is P18, not this
class; the collapsed three-way `hollow, empty, or over` line is P18, not
this class; a refusal on a sweep with zero never-served units is not this
class. Reaction: the planner's `fitted under the cap` line.
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

_SERVICE = ("  [stable] [phase-c/dispatch] service order: 7 never-served unit(s) "
            "rank before served; 5 last-graded flat sink")
_SERVICE_NONE = ("  [stable] [phase-c/dispatch] service order: 0 never-served "
                 "unit(s) rank before served; 8 last-graded flat sink")
_REFUSE = ("  [phase-c/session] no slice planned (32 failing test(s) — 1 over "
           "SLICE_SESSION_MAX_UNITS=32 (60 unit(s)))")
_EXPAND = "  [phase-c/session] locator-only w.rs::fmt expanded to 4 tree site(s)"


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p21_refusal_with_never_served_waiting_is_the_class() -> None:
    run = _run(_SERVICE, "x", _REFUSE)
    hits = census.detect_p21(run, [])
    assert len(hits) == 1
    assert "7 never-served" in hits[0]["evidence"]
    assert hits[0]["refs"] == ["log:3", "log:1"]
    assert census.detect_p18(run, []) == []


def test_p21_after_a_locator_expansion_is_p18_not_p21() -> None:
    run = _run(_SERVICE, _EXPAND, _REFUSE)
    assert census.detect_p21(run, []) == []
    assert census.detect_p18(run, [])


def test_p21_collapsed_three_way_is_p18_not_p21() -> None:
    """Run 110993-6efa23be: `or over`, not `N over`."""
    line = (
        "  [phase-c/session] no slice planned "
        "(3 failing test(s) — hollow, empty, or over SLICE_SESSION_MAX_UNITS=32)"
    )
    run = _run(_SERVICE, line)
    assert census.detect_p21(run, []) == []
    assert census.detect_p18(run, [])


def test_p21_expansion_in_a_prior_fill_does_not_block() -> None:
    """Run 57079-a5f83827 expanded at iter 5 and split-refused after a
    later service-order. A 12-line window happened to agree; fill scope
    is the rule."""
    run = _run(_SERVICE, _EXPAND, _SERVICE, _REFUSE)
    hits = census.detect_p21(run, [])
    assert len(hits) == 1
    assert "7 never-served" in hits[0]["evidence"]


def test_p21_with_no_never_served_is_not_the_class() -> None:
    assert census.detect_p21(_run(_SERVICE_NONE, _REFUSE), []) == []
    assert census.detect_p21(_run(_REFUSE), []) == []


def test_p21_fitted_is_the_reaction() -> None:
    assert any(m == "session-fitted" for _, m in census.REACTIONS["P21"])
    assert "P21" in census.DETECTORS and "P21" in census.CLASS_NAMES
