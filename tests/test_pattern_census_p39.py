"""P39 unevidenced stub narrowing.

The deferral is printed after the iteration summary, and the stub-hold
line is printed before that summary. Run `1038428-3372223f` (2026-09-23)
iterations 4-6 deferred with no hold line on the sweep; iterations 1 and 3
of the same run deferred after a hold line. Clearing the hold flag on the
summary counted both. A deferral counts only when the summary before it
saw no hold line. The shape here is invented.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p39", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _summary(n: int) -> str:
    return (f"  [c] [phase-c/tdd] iter {n}: cargo test rc=101 (0.4s); "
            f"pass=1 fail=1 oracle=2 (improved=False, stall=0/0); "
            f"frontier=3 fn(s) (merged), 0 skipped as exhausted")


def _hold(n: int) -> str:
    return (f"  [c] [phase-c/crash] iter {n}: 1 still-stubbed fn(s) hold "
            f"2 failing test(s) at their first failure")


def _deferral() -> str:
    return ("  [phase-c/impl] cluster c: deferring the impl route this iter "
            "— 1 frontier stub(s) it never attempts")


def _run(*lines: str) -> object:
    run = census.Run("r", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def _fills(hits: list[dict]) -> int:
    assert len(hits) == 1
    return int(hits[0]["evidence"].split()[0])


def test_a_deferral_after_a_hold_and_its_summary_is_silent() -> None:
    """The pre-fix detector cleared the hold flag on the summary, so this
    sandwich was a hit. The summary is the sweep the deferral belongs to."""
    assert census.detect_p39(
        _run(_hold(1), _summary(1), "  noise", _deferral()), []) == []


def test_a_deferral_after_a_summary_with_no_hold_counts() -> None:
    hits = census.detect_p39(_run(_summary(4), _deferral(), _deferral()), [])
    assert _fills(hits) == 2


def test_a_deferral_before_any_summary_counts() -> None:
    hits = census.detect_p39(_run(_deferral()), [])
    assert _fills(hits) == 1


def test_an_earlier_held_sweep_does_not_silence_the_next() -> None:
    hits = census.detect_p39(_run(
        _hold(1), _summary(1), _deferral(),
        _summary(4), _deferral()), [])
    assert _fills(hits) == 1


def test_p39_is_registered() -> None:
    assert "P39" in census.DETECTORS and "P39" in census.CLASS_NAMES
