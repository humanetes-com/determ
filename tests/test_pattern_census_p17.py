"""P17 — fill-class histogram. The detector reads the iter-line token
``classes: cashed=N wrong=N …``; pre-emitter logs have no signature.

Nothing is gated on the dominant class. The row exists so a later gate
has a measured distribution rather than a guessed threshold.
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


def test_p17_histogram_line_names_the_dominant_class() -> None:
    """Plan sentence: a WRONG_BODY-dominated iteration is a census row."""
    line = (
        "  [phase-c/tdd] iter 2: cargo test rc=101; pass=83 fail=44 "
        "oracle=127 (improved=False, stall=1/0); frontier=47; "
        "classes: cashed=1 wrong=8 mis=6 unreachable=0 dropped=0 compile=4"
    )
    hits = census.detect_p17(_run(line), [])
    assert len(hits) == 1
    assert "dominant wrong" in hits[0]["evidence"]
    assert "mis=6" in hits[0]["evidence"]


def test_p17_pre_emitter_log_is_not_the_class() -> None:
    """Negative control: a 463713-shaped iter line has no classes token."""
    line = (
        "  [phase-c/tdd] iter 2: cargo test rc=101; pass=75 fail=52 "
        "oracle=127 (improved=False, stall=0/0); frontier=43 fn(s) "
        "(merged), 0 skipped as exhausted"
    )
    assert census.detect_p17(_run(line), []) == []
    assert census.RE_FILL_CLASSES.search(line) is None
