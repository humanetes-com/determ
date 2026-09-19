"""One typed outcome per test, the premise a fill is planned from, and how
far a run got.

The live fixture is run `3383938-c5d24499` (2026-09-19): the entry tree
(`construct` stubbed) against the same tree after file-first attempt 1's 13
kept bodies, both captured with isolation, sites resolved by the release
extractor on each tree (`tests/fixtures/premise/3383938_entry_vs_kept.json`).
The plan's sentences: every one of the 141 failing tests' first failure
moved, and every one of them was a run truncated inside the crate.
Invented shapes cover the parser and the identity rules.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_outcome import (
    RunKind,
    SiteKind,
    TestOutcome,
    outcomes_of,
    premise_moved,
    premise_of,
    run_kind,
    site_kind_of,
    truncated_failing,
)
from pipeline_core.domain.test_results import ABORT_MARK

_FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "premise"
                   / "3383938_entry_vs_kept.json").read_text(encoding="utf-8"))


def _tree(t: str):
    tree = _FIX["trees"][t]
    outcomes = {n: TestOutcome(n, p, f, ln, a, s)
                for n, p, f, ln, a, s in tree["outcomes"]}
    sites = tree["sites"]

    def locate(file: str, line: int) -> FnSite | None:
        hit = sites.get(f"{file}:{line}")
        return FnSite(*hit) if hit else None
    return outcomes, locate


def test_live_the_first_keep_moved_every_failing_tests_ground() -> None:
    oe, le = _tree("E")
    ok, lk = _tree("K")
    pe, pk = premise_of(oe, le), premise_of(ok, lk)
    moved = premise_moved(pe, pk)
    failing = [n for n, o in oe.items() if not o.passed]
    assert len(failing) == 141 and len(moved) == 141
    assert premise_moved(pe, pe) == ()
    sites = collections.Counter(v[1] for v in pk.values() if not v[0])
    assert sites[("fn", "src/error.rs", "object_ref", "")] == 111
    assert sites[("fn", "src/error.rs", "vtable", "")] == 13
    assert {v[1] for v in pe.values() if not v[0]} == {
        ("fn", "src/error.rs", "construct", "impl Error")}


def test_live_every_failing_run_was_truncated_inside_the_crate() -> None:
    oe, _ = _tree("E")
    ok, _ = _tree("K")
    assert len(truncated_failing(oe)) == 141
    assert len(truncated_failing(ok)) == 141
    assert collections.Counter(run_kind(o) for o in oe.values()) == {
        RunKind.TRUNCATED: 141, RunKind.COMPLETE: 53}


def test_site_kinds() -> None:
    assert site_kind_of("src/error.rs") is SiteKind.CRATE
    assert site_kind_of("tests/test_a.rs") is SiteKind.TEST
    assert site_kind_of("src/fmt.gen_tests_2.rs") is SiteKind.TEST
    assert site_kind_of("/rustc/abc/library/core/src/panicking.rs") is SiteKind.RUNTIME
    assert site_kind_of(None) is SiteKind.NONE


def test_outcomes_and_run_kinds_from_a_capture() -> None:
    capture = "\n".join([
        "running 4 tests",
        "test ok_one ... ok",
        "test asserted ... FAILED",
        "test in_crate ... FAILED",
        "test died ... FAILED",
        "", "failures:", "",
        "---- asserted stdout ----",
        "thread 'asserted' panicked at tests/t.rs:7:5:",
        "assertion `left == right` failed", "",
        "---- in_crate stdout ----",
        "thread 'in_crate' panicked at src/a.rs:3:5:",
        "not implemented", "",
        "---- died stdout ----",
        ABORT_MARK,
        "thread 'died' panicked at tests/t.rs:9:5:", "own",
        "thread 'died' panicked at src/a.rs:12:5:", "not implemented", "",
        "failures:", "    asserted", "    in_crate", "    died", "",
        "test result: FAILED. 1 passed; 3 failed; 0 ignored; 0 measured; "
        "0 filtered out"])
    o = outcomes_of(capture)
    assert o["ok_one"].passed and run_kind(o["ok_one"]) is RunKind.COMPLETE
    assert run_kind(o["asserted"]) is RunKind.ASSERTED
    assert run_kind(o["in_crate"]) is RunKind.TRUNCATED
    # An aborted process is truncated even when its first panic was in test
    # source: the abort cut whatever came after.
    assert o["died"].aborted and run_kind(o["died"]) is RunKind.TRUNCATED
    assert truncated_failing(o) == {"in_crate", "died"}


def test_a_body_growing_above_a_site_is_not_a_move() -> None:
    """Crate sites are the function they lie in: the same stub two lines
    further down after a splice above it is the same ground. Negative
    control: with no locator the site is the file alone, which also does
    not move — and a raw line identity would have."""
    before = {"t": TestOutcome("t", False, "src/a.rs", 40)}
    after = {"t": TestOutcome("t", False, "src/a.rs", 47)}
    same_fn = {("src/a.rs", 40): FnSite("src/a.rs", "vt", ""),
               ("src/a.rs", 47): FnSite("src/a.rs", "vt", "")}

    def locate(f: str, ln: int) -> FnSite | None:
        return same_fn.get((f, ln))
    assert premise_moved(premise_of(before, locate),
                         premise_of(after, locate)) == ()
    other_fn = {("src/a.rs", 40): FnSite("src/a.rs", "make", ""),
                ("src/a.rs", 47): FnSite("src/a.rs", "vt", "")}
    assert premise_moved(
        premise_of(before, lambda f, ln: other_fn.get((f, ln))),
        premise_of(after, lambda f, ln: other_fn.get((f, ln)))) == ("t",)


def test_doc_test_renames_and_absent_tests_are_not_moves() -> None:
    a = {"src/a.rs - G::get (line 40)": TestOutcome(
        "src/a.rs - G::get (line 40)", False, "src/a.rs", 5),
        "gone": TestOutcome("gone", True)}
    b = {"src/a.rs - G::get (line 47)": TestOutcome(
        "src/a.rs - G::get (line 47)", False, "src/a.rs", 5)}
    assert premise_moved(premise_of(a), premise_of(b)) == ()
    # A verdict change is a move.
    c = {"src/a.rs - G::get (line 47)": TestOutcome(
        "src/a.rs - G::get (line 47)", True)}
    assert premise_moved(premise_of(a), premise_of(c)) == (
        "src/a.rs - G::get#0",)
