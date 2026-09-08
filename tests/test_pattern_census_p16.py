"""P16 — regression-derived prescription — the screen's log signature and
the store scan.

The detector reads logs and the retrospectives store. These pins are the
three line shapes that carry `retro guidance withheld (regression_scoped)`
(the per-block judge site, the per-fn judge site, the prompt-build
retirement line), the SILENT shape (an honoured line on a unit whose record
is regression-derived and unstamped), and the standing-state scan on a
copy of the live record.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()
_KEY = "src/ensure.rs::*impl*::BothDebugfor(A,B)"
# The live record's shape (tmp/anyhow/.phase_c_logs/retrospectives.json,
# 2026-08-26): compiled, thirteen red, convicted by nothing, preview cut.
_LIVE_RECORD = {
    "category": "WRONG_TYPE_CHOICE",
    "guidance": "Drop the fixed 40-byte SmallBuf and format into an "
                "allocating Write so the debug output is never truncated",
    "error_count": 0, "failing_test_count": 13, "fully_exhausted": False,
    "last_error_preview": "This compiled, but it REGRESSED previously-passing "
                          "test(s): ['test_as', 'test_atom', 'test_high_pre",
    "cluster": "stable", "convicted_by": [],
}


def _run(*lines: str) -> object:
    run = census.Run("t", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p16_regex_matches_the_three_withheld_line_shapes() -> None:
    impl = ("  [phase-c/impl] src/ensure.rs::BothDebugfor(A,B): retro guidance "
            "was written from a regression of test(s) this block does not "
            "answer for — retro guidance withheld (regression_scoped), "
            "exhaustion not honoured (regression-scoped).")
    per_fn = ("  [phase-c/per-fn] src/ensure.rs::render []: retro guidance "
              "was written from a regression of test(s) this fn does not "
              "answer for — retro guidance withheld (regression_scoped), "
              "exhaustion not honoured (regression-scoped).")
    retired = (f"  [phase-c/retro-retired] {_KEY}: category=WRONG_TYPE_CHOICE "
               "rounds=1 was written from a regression of 10 test(s) this unit "
               "does not answer for (['test_as', 'test_atom']) — retro "
               "guidance withheld (regression_scoped): Drop the fixed 40-byte")
    units = [census.RE_RETRO_REGRESSION_SCOPED.search(ln)["unit"]
             for ln in (impl, per_fn, retired)]
    assert units == ["src/ensure.rs::BothDebugfor(A,B)", "src/ensure.rs::render", _KEY]
    # NEGATIVE CONTROL: the other retirement class does not match.
    other = (f"  [phase-c/retro-retired] {_KEY}: category=X rounds=1 prescribes "
             "a body that does no work while the unit answers for failing "
             "test(s) (convicted_by=['t']) — NOT injected: replace the body")
    assert census.RE_RETRO_REGRESSION_SCOPED.search(other) is None


def test_p16_silent_when_an_unstamped_regression_record_steers_and_reacted_when_withheld(
) -> None:
    census.P16_UNSTAMPED_UNITS = {_KEY}
    honoured = (f"  [phase-c/retro-honoured] {_KEY}: category=WRONG_TYPE_CHOICE "
                "rounds=1 NO tree premise guidance: Drop the fixed 40-byte SmallBuf")
    run = _run(honoured, honoured)
    hits = census.detect_p16(run, [])
    assert len(hits) == 1
    assert hits[0]["evidence"].startswith(f"{_KEY} steered 2 prompt(s)")
    assert "NO `regressed` stamp" in hits[0]["evidence"]
    assert run.reactions_for("P16", after=0) == []          # SILENT
    withheld = (f"  [phase-c/retro-retired] {_KEY}: category=WRONG_TYPE_CHOICE "
                "rounds=1 was written from a regression of 10 test(s) this unit "
                "does not answer for (['test_as']) — retro guidance withheld "
                "(regression_scoped): Drop the fixed 40-byte")
    run2 = _run(withheld)
    hits2 = census.detect_p16(run2, [])
    assert len(hits2) == 1 and "withheld 1x" in hits2[0]["evidence"]
    assert run2.reactions_for("P16", after=0) == [("regression-scoped", "log:1")]
    # NEGATIVE CONTROL: an honoured line on a unit that is not
    # regression-derived is not this class.
    census.P16_UNSTAMPED_UNITS = set()
    assert census.detect_p16(_run(honoured), []) == []
    assert "P16" in census.DETECTORS and "P16" in census.CLASS_NAMES


def test_p16_store_scan_lists_regression_derived_records_and_their_stamp(tmp_path: Path) -> None:
    store = tmp_path / "tmp" / "anyhow" / ".phase_c_logs" / "retrospectives.json"
    store.parent.mkdir(parents=True)
    compiled_clean = {**_LIVE_RECORD, "last_error_preview": "This body COMPILES "
                      "but its attributed test(s) below still FAIL."}
    compile_taught = {**_LIVE_RECORD, "error_count": 2}
    stamped = {**_LIVE_RECORD, "regressed": ["test_as", "test_atom"]}
    store.write_text(json.dumps({
        "__migrations__": ["comparative_gate_2026_07"],
        _KEY: _LIVE_RECORD,
        "src/ensure.rs::*impl*::WriteforBuf": stamped,
        "src/lib.rs::*impl*::Boolforbool": compiled_clean,
        "src/context.rs::*impl*::StdErrorforE": compile_taught,
    }))
    rows = census.regression_derived_records(tmp_path)
    assert [(r["unit"], r["regressed"]) for r in rows] == [
        (_KEY, None),
        ("src/ensure.rs::*impl*::WriteforBuf", ["test_as", "test_atom"]),
    ]
    assert rows[0]["failing_tests"] == 13 and rows[0]["convicted_by"] == []
    assert rows[0]["guidance"].startswith("Drop the fixed 40-byte")
    assert rows[0]["store"] == "tmp/anyhow/.phase_c_logs/retrospectives.json"
    # No store, no rows.
    assert census.regression_derived_records(tmp_path / "nowhere") == []


# --- the crash-earned class: both live records of run 2585062-494e630e ------
#
# Shapes copied from `tmp/anyhow/.phase_c_logs/retrospectives.json`
# (2026-09-08 04:50 UTC, guidance trimmed). Pre-fix the detector keyed on the
# `REGRESSED previously-passing test(s)` head only and printed `no
# regression-derived record in any store` while these two sat on disk, one
# of them `fully_exhausted`.
_LIVE_ABORT = {
    "category": "WRONG_TYPE_CHOICE",
    "guidance": "Move the Box into an owned raw pointer with Box::into_raw",
    "error_count": 0, "failing_test_count": 29, "fully_exhausted": True,
    "last_error_preview": "Your body ABORTED the test process. These test(s) "
                          "produced a verdict BEFORE your change",
    "cluster": "stable", "convicted_by": ["boxed_error_source_none"],
    "regressed": [], "blocked_on": [],
}
_LIVE_CRASH = {
    "category": "WRONG_TYPE_CHOICE",
    "guidance": "the constructor should just adopt the supplied raw pointer",
    "error_count": 0, "failing_test_count": 4, "fully_exhausted": False,
    "last_error_preview": "This made test binaries ['tests/context_gen_1.rs'] "
                          "CRASH before reporting results",
    "cluster": "stable", "convicted_by": [], "regressed": [],
    "guidance_from_hollow_tree": True, "blocked_on": ["boxed", "new"],
}


def test_p16_store_scan_lists_crash_earned_records_as_unstamped(tmp_path: Path) -> None:
    store = tmp_path / "tmp" / "anyhow" / ".phase_c_logs"
    store.mkdir(parents=True)
    (store / "retrospectives.json").write_text(json.dumps({
        "src/ptr.rs::new::Own<T>": _LIVE_ABORT,
        "src/ptr.rs::*impl*::Own<T>": _LIVE_CRASH,
        _KEY: _LIVE_RECORD,
        # NEGATIVE CONTROLS: a compile-taught record with the crash head is
        # the compiler's verdict; a stamped crash record is screened.
        "src/x.rs::f::": {**_LIVE_ABORT, "error_count": 3},
        "src/y.rs::g::": {**_LIVE_ABORT, "crash_stub_sites": ["src/error.rs:669"]},
    }), encoding="utf-8")
    rows = {r["unit"]: r for r in census.regression_derived_records(tmp_path)}
    assert set(rows) == {"src/ptr.rs::new::Own<T>", "src/ptr.rs::*impl*::Own<T>",
                         _KEY, "src/y.rs::g::"}
    abort = rows["src/ptr.rs::new::Own<T>"]
    assert abort["kind"] == "crash" and abort["exhausted"] is True
    assert abort["stamp"] is None and abort["regressed"] is None
    crash = rows["src/ptr.rs::*impl*::Own<T>"]
    assert crash["kind"] == "crash" and crash["stamp"] is None
    assert rows[_KEY]["kind"] == "regression"
    assert rows["src/y.rs::g::"]["stamp"] == ["src/error.rs:669"]
    # The unstamped population the SILENT shape keys on holds both live
    # records and the regression one, not the stamped crash record.
    census.P16_UNSTAMPED_UNITS = None
    try:
        import unittest.mock as um
        with um.patch.object(census, "regression_derived_records",
                             lambda root: list(rows.values())):
            assert census._p16_unstamped_units() == {
                "src/ptr.rs::new::Own<T>", "src/ptr.rs::*impl*::Own<T>", _KEY}
    finally:
        census.P16_UNSTAMPED_UNITS = None
