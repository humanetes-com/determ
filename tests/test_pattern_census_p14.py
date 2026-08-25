"""P14 — compliance over compiler — keys consecutive identical diagnostics.

The detector reads logs, not events. These pins are the two line shapes it
parses (per-impl attempt + first diagnostic; file-first revert with codes
and first diagnostic, added 2026-08-24) and the identity it claims: same
unit, consecutive attempts, identical (code, header symbols). kind.rs::Trait
and error.rs::Error are different units, so they do not hash together.
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


def test_p14_file_first_revert_line_matches_the_impl_fill_format() -> None:
    """The 2026-08-24 observability completion: file-first revert carries
    codes + first diagnostic so three identical E0277s are visible. The
    regex must match the line impl_fill actually prints."""
    line = (
        "  [phase-c/file] src/error.rs: reverted (compile failed; "
        "codes=['E0277'] first: error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied) — "
        "retrying file-first once more."
    )
    m = census.RE_FILE_REVERT_DIAG.search(line)
    assert m is not None
    assert m["unit"] == "src/error.rs"
    assert m["code"] == "E0277"
    assert m["msg"].startswith("the trait bound")


def test_p14_three_identical_file_first_reverts_are_one_hit() -> None:
    """The defining instance: three file-first reverts, same unit, same
    primary diagnostic. One hit, key (unit, code, header symbols)."""
    diag = (
        "  [phase-c/file] src/error.rs: reverted (compile failed; "
        "codes=['E0277'] first: error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied) — "
        "retrying file-first once more."
    )
    run = _run(diag, diag, diag.replace("retrying file-first once more.",
                                        "falling back to per-block fills."))
    hits = census.detect_p14(run, [])
    assert len(hits) == 1
    assert hits[0]["key"][0] == "src/error.rs"
    assert hits[0]["key"][1] == "E0277"
    assert "Error: core::error::Error" in hits[0]["key"][2]


def test_p14_per_impl_consecutive_identical_diagnostics_hit() -> None:
    run = _run(
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 1); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=0.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 2); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=1.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
    )
    hits = census.detect_p14(run, [])
    assert len(hits) == 1
    assert hits[0]["key"][0] == "src/error.rs::Error"
    assert "1" in hits[0]["evidence"] and "2" in hits[0]["evidence"]


def test_p14_different_units_do_not_hash_together() -> None:
    """kind.rs::Trait E0277 is the same bound family, a different unit.
    P14 is per-unit; a chronic groups by key including the unit."""
    run = _run(
        "  [phase-c/impl] src/kind.rs::Trait: compile failed (attempt 1); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=0.",
        "  [phase-c/impl] src/kind.rs::Trait:   error[E0277]: the trait bound "
        "`E: core::error::Error` is not satisfied",
        "  [phase-c/impl] src/kind.rs::Trait: compile failed (attempt 2); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=1.",
        "  [phase-c/impl] src/kind.rs::Trait:   error[E0277]: the trait bound "
        "`E: core::error::Error` is not satisfied",
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 1); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=0.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 2); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=1.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
    )
    hits = census.detect_p14(run, [])
    units = {h["key"][0] for h in hits}
    assert units == {"src/kind.rs::Trait", "src/error.rs::Error"}
    keys = {tuple(h["key"][2]) for h in hits}
    assert len(keys) == 2


def test_p14_a_single_failure_is_not_the_class() -> None:
    run = _run(
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 1); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=0.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
    )
    assert census.detect_p14(run, []) == []


def test_p14_changed_diagnostic_breaks_the_streak() -> None:
    run = _run(
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 1); "
        "errs=1 compile err(s) codes=['E0277'] floor=1 stall=0.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0277]: the trait bound "
        "`Error: core::error::Error` is not satisfied",
        "  [phase-c/impl] src/error.rs::Error: compile failed (attempt 2); "
        "errs=1 compile err(s) codes=['E0308'] floor=1 stall=0.",
        "  [phase-c/impl] src/error.rs::Error:   error[E0308]: mismatched types",
    )
    assert census.detect_p14(run, []) == []
