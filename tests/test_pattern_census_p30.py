"""P30 spent pair first.

Sentence: a fill schedule line whose ordered runs hold a `grace_spent` pair
before a `never_served` run is a hit; the same line with the never-served
runs first, or with the never-served run only in the deferred list, is not.
The hit line is the recorded fill-1 line of run `3893442-cb547388`
(2026-09-19), trimmed to its first four runs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p30", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


_RECORDED = (
    "  [phase-c/schedule] fill 1: 15 run(s) — 1. session[2 test(s), 12 "
    "unit(s)] #4 (never_served) share 15m; 2. pair src/error.rs::fmt + "
    "src/fmt.rs::debug (grace_spent) share 7m; 3. pair src/error.rs::fmt + "
    "src/fmt.rs::display (grace_spent) share 7m; 4. session[1 test(s), 29 "
    "unit(s)] #0 (never_served; grace spent: pair_wait; after pair "
    "src/error.rs::fmt + src/fmt.rs::debug) share 15m — deferred: "
    "session[2 test(s), 27 unit(s)] #5 (kind_cap); budget 200 min")


def test_p30_hits_a_spent_pair_ahead_of_a_never_served_run() -> None:
    hits = census.detect_p30(_run(_RECORDED), [])
    assert len(hits) == 1 and "fill 1" in hits[0]["evidence"]


def test_p30_quiet_when_the_never_served_runs_go_first() -> None:
    line = ("  [phase-c/schedule] fill 1: 3 run(s) — 1. session[1 test(s), 29 "
            "unit(s)] #0 (never_served) share 15m; 2. file src/x.rs "
            "(never_served) share 15m; 3. pair src/error.rs::fmt + "
            "src/fmt.rs::debug (grace_spent) share 7m — deferred: pair a + b "
            "(rebuy_flat); budget 200 min")
    assert census.detect_p30(_run(line), []) == []


def test_p30_ignores_the_deferred_list() -> None:
    line = ("  [phase-c/schedule] fill 2: 1 run(s) — 1. pair a + b (grace_spent) "
            "share 7m — deferred: session[1 test(s), 3 unit(s)] #0 "
            "(never_served) (kind_cap); budget 200 min")
    assert census.detect_p30(_run(line), []) == []
