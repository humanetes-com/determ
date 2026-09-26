"""P43 stack overflow blamed nobody.

Run `38934-2999cc94` (2026-09-26): 36 `[phase-c/crash] … AddressSanitizer:
stack-overflow — blamed NOBODY (origin not attributed in-crate)` lines over
sweeps 5-7, while each report held the recursion's cycle. The detector counts
that pre-fix line; the post-fix line names the cycle's fns and carries
`fault=exhaustion`, and a use-after-free whose origin is outside the crate is
not this class. The shape here is invented.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p43", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_PRE = ("  [c] [phase-c/crash] tests/t_{n}.rs::test_{n} AddressSanitizer: "
        "stack-overflow — blamed NOBODY (origin not attributed in-crate) "
        "(provenance=frame_derived)")
_POST = ("  [c] [phase-c/crash] tests/t_{n}.rs::test_{n} AddressSanitizer: "
         "stack-overflow — blamed src/a.rs::f, src/b.rs::g "
         "(fault=exhaustion, provenance=frame_derived)")
_UAF = ("  [c] [phase-c/crash] tests/t_{n}.rs::test_{n} AddressSanitizer: "
        "heap-use-after-free — blamed NOBODY (origin not attributed in-crate) "
        "(provenance=frame_derived)")


def _run(*lines: str) -> object:
    run = census.Run("r", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_the_pre_fix_reading_is_a_hit_and_counts_the_lines() -> None:
    hits = census.detect_p43(_run(*[_PRE.format(n=i) for i in range(3)]), [])
    assert len(hits) == 1 and "3 stack-overflow report(s)" in hits[0]["evidence"]


def test_negative_control_the_post_fix_line_and_other_kinds_are_not() -> None:
    assert census.detect_p43(_run(*[_POST.format(n=i) for i in range(4)]), []) == []
    assert census.detect_p43(_run(*[_UAF.format(n=i) for i in range(4)]), []) == []
    assert census.detect_p43(_run(_PRE.format(n=0)), []) == []      # one line
