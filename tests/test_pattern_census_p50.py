"""P50 clean gain reverted, on the recorded fill.

Run `14495-a26ad4ea` (2026-10-15, cluster `stable`) fill 6: the file route
read `pass 120 -> 127 (net +7) … no regression` on each of three
`src/error.rs` attempts and reverted each on "no strict improvement" — the
joint keep compared bare names, and one binary's copy passed while the
other binary's copy of the same name stayed red. A clean reading followed
by a revert that names `newly red:` is the same class. A flat reading, a
revert that only names an earlier panic or an abort, and a reading that
already lists `newly red:`, are not.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p50", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_READ = ("  [phase-c/fill-reading] pass 120 -> 127 (net +7); executed 144 -> "
         "144; 54 binaries measured; no regression in the measured scope")
_REVERT = ("  [phase-c/file] src/error.rs: reverted (U2 test failure (no strict "
           "improvement)) — retrying file-first once more.")


def _run(lines: list[str]):
    run = census.Run("14495-a26ad4ea", "5e8a672", "5e8a6720738b", "", "log:1")
    run.lines = [(f"log:{i}", t) for i, t in enumerate(lines, 1)]
    return run


def test_the_recorded_fill_is_a_hit() -> None:
    hits = census.detect_p50(_run([_READ, _REVERT, "  [phase-c/file] "
                                   "src/error.rs: file-first fill (attempt 2/3).",
                                   _READ, _REVERT]), [])
    assert hits and hits[0]["evidence"].startswith("2 joint revert(s)")


def test_a_clean_reading_reverted_for_newly_red_is_a_hit() -> None:
    named = _REVERT.replace("improvement))",
                            "improvement; newly red: t_other))")
    hits = census.detect_p50(_run([_READ, named]), [])
    assert hits and hits[0]["evidence"].startswith("1 joint revert")


def test_a_flat_reading_or_another_clause_or_an_already_red_reading_is_not_a_hit() -> None:
    flat = _READ.replace("127 (net +7)", "120 (net +0)")
    earlier = _REVERT.replace("improvement))",
                              "improvement; panics earlier: t_other))")
    aborted = _REVERT.replace("improvement))",
                              "improvement; aborted: t_other))")
    already = _READ.replace(
        "54 binaries measured; no regression",
        "54 binaries measured; newly red: t_other; no regression")
    named = _REVERT.replace("improvement))",
                            "improvement; newly red: t_other))")
    assert census.detect_p50(_run([flat, _REVERT]), []) == []
    assert census.detect_p50(_run([_READ, earlier]), []) == []
    assert census.detect_p50(_run([_READ, aborted]), []) == []
    assert census.detect_p50(_run([already, named]), []) == []
