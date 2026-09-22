"""P34 abandoned block.

Sentence: a fix loop whose COMPILE-error floor never reached zero and that
stopped spending on the unit is a hit, keyed on the unit ALONE — a block that
stalls under a different diagnostic each run is one chronic, not one class per
error code.

Which number is a compile floor differs by line, and that is what this module
pins. `[phase-c/impl]` / `[phase-c/per-fn]` render the floor (`N compile
err(s)` vs `compiles, N failing test(s)`); `[improvement_loop/…]` prints the
raw metric, `signatures * 10_000 + failing tests`, so a floor of 1 there is a
body that COMPILES with one failing test and a floor of 10018 is one signature
with eighteen. The line shapes are the recorded ones of runs
`955468-7b947bd0` (2026-08-29, E0277), `2973-66e50ad9` (2026-09-04, E0425) and
`334902-2774708c` (E0308) — three codes, one block.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p34", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _run(run_id: str, *lines: str) -> object:
    run = census.Run(run_id, "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


_IMPL = ("  [phase-c/impl] src/a.rs::Thing: floor stalled at 2 compile err(s) "
         "for 3 iter(s) codes=['E0277'] — stopping this block.")
_PER_FN_ERRORS = ("  [phase-c/per-fn] maker: floor stalled at 2 errors for "
                  "3 iter(s) — stopping fix loop (attempt 4).")
_LOOP_COMPILE = ("  [improvement_loop/impl:src/b.rs::impl Held] floor stalled "
                 "at 10018 for 2 iter(s) — stopping.")
_T2 = ("  Inner loop floor stalled at 4 errors for 2 regen(s) — escalating to "
       "outer loop early.")

# NEGATIVE CONTROLS, all three shapes of "this is not a compile floor":
# the body compiles and its tests are what did not turn …
_RENDERED_GREEN = ("  [phase-c/per-fn] maker: floor stalled at compiles, 2 "
                   "failing test(s) for 3 iter(s) — stopping fix loop (attempt 4).")
# … the same thing on the line that prints the raw metric (1 = one failing
# test, zero compile signatures) …
_LOOP_GREEN = ("  [improvement_loop/impl:src/b.rs::impl Held] floor stalled at "
               "1 for 2 iter(s) — stopping.")
# … and a floor of zero, which is nothing at all.
_ZERO = ("  [phase-c/impl] src/a.rs::Thing: floor stalled at 0 compile err(s) "
         "for 3 iter(s) codes=— — stopping this block.")
# A loop name is not a block: it names no unit and opens no chronic row.
_LOOP_NAMED = ("  [improvement_loop/generate_fix_loop] floor stalled at 10001 "
               "for 3 iter(s) — stopping.")


def test_p34_hits_every_abandoned_compile_floor_shape() -> None:
    hits = census.detect_p34(
        _run("r1", _IMPL, _PER_FN_ERRORS, _LOOP_COMPILE, _T2), [])
    assert [h["key"][0] for h in hits] == [
        "src/a.rs::Thing", "maker", "src/b.rs::impl Held", ""]
    assert "dropped without ever compiling" in hits[0]["evidence"]
    # The metric line is decoded into its two tiers rather than printed raw.
    assert "1 compile signature(s) + 18 failing test(s)" in hits[2]["evidence"]
    assert "t2 inner loop, no block" in hits[3]["evidence"]


def test_p34_does_not_hit_a_floor_that_compiles() -> None:
    assert census.detect_p34(
        _run("r1", _RENDERED_GREEN, _LOOP_GREEN, _ZERO), []) == []


def test_p34_keys_one_block_across_three_diagnostics() -> None:
    """The re-key: the class was recorded against one diagnostic and read as
    closed when that code stopped appearing, while the same block went on
    stalling under others. The key carries no code, so the cross-run table
    names the block once."""
    runs = [
        _run("r1", _IMPL),
        _run("r2", _IMPL.replace("E0277", "E0425").replace("at 2", "at 1")),
        _run("r3", _IMPL.replace("E0277", "E0308")),
    ]
    chron = census.chronics(runs)["abandoned_block"]
    assert [(e["what"], e["runs"]) for e in chron] == [
        ("src/a.rs::Thing", ["r1", "r2", "r3"])]

    # NEGATIVE CONTROL: keyed WITH the diagnostic — the shape the other
    # compliance detector uses — the same three runs are three separate
    # one-run rows and no chronic shows.
    keyed: dict[tuple[str, str], list[str]] = {}
    for r in runs:
        for _ref, unit, tail in census._floor_stalls(r):
            keyed.setdefault((unit, tail), []).append(r.run_id)
    assert max(len(v) for v in keyed.values()) == 1


def test_p34_chronic_ignores_a_stall_that_names_no_block() -> None:
    """A whole-loop abandonment is an instance of the class and is reported,
    but it names no block, so it must not open a chronic row of its own — on
    the recorded logs the whole-file loop's own name would otherwise be the
    second-largest chronic in the table."""
    runs = [_run("r1", _LOOP_NAMED, _T2), _run("r2", _LOOP_NAMED, _T2)]
    assert [h["key"][0] for h in census.detect_p34(runs[0], [])] == ["", ""]
    assert census.chronics(runs)["abandoned_block"] == []
