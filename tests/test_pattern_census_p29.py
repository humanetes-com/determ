"""P29 parked stub.

Sentence: an iteration whose blocker histogram ranks a stub first and whose
fill plan opens no `per-fn N stub(s)` run with N >= 1 is a hit; the same
iteration with the stub run in the plan is not. Lines are the recorded ones
of run `3453127-045b05b1` (2026-09-19), iters 3 and 5.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p29", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_HIST = ("  [stable] [phase-c/dispatch] blocker histogram: src/error.rs::vtable "
         "(7) rank first (crash evidence still outranks)")


def _iter(n: int) -> str:
    return (f"  [stable] [phase-c/tdd] iter {n}: cargo test rc=101 (13.6s); "
            "pass=38 fail=146 oracle=184 (improved=False, stall=0/0)")


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p29_hits_a_fill_that_opens_no_stub_run() -> None:
    hits = census.detect_p29(_run(
        _HIST, _iter(3),
        "  [phase-c/schedule] fill 3: 1 run(s) — 1. pair src/error.rs::fmt + "
        "src/fmt.rs::debug (grace_spent) share 200m — deferred: per-fn 0 "
        "stub(s), 15 re-fill(s) (pair_wait)"), [])
    assert len(hits) == 1
    assert "iter 3 parked src/error.rs::vtable" in hits[0]["evidence"]


def test_p29_quiet_when_the_fill_opens_the_stub() -> None:
    assert census.detect_p29(_run(
        _HIST, _iter(5),
        "  [phase-c/schedule] fill 5: 1 run(s) — 1. per-fn 1 stub(s), 0 "
        "re-fill(s) (never_served; 1 stub(s)) share 200m"), []) == []


def test_p29_quiet_without_a_histogram() -> None:
    assert census.detect_p29(_run(
        _iter(3),
        "  [phase-c/schedule] fill 3: 1 run(s) — 1. pair a + b share 200m"),
        []) == []
