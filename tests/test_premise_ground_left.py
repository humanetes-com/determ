"""A claim is refused only when its whole planned ground moved.

Recorded fixture: run `116341-24c76a97` (2026-09-25, cluster `stable`),
fill 6 — every route-ledger row of iteration 6 with its planned tests, and
the tests the fill's keeps moved (`phase_c.fill.premise_moved`, iteration
6: `test_downcast`, `test_downcast_mut`, `test_too_long`). The keeps of the
per-fn run moved the first two; the fill then deferred both formatter pairs
`premise_moved` with 1 of their 54 planned tests moved (2 by the fill's
end, the set this fixture carries), and the error.rs file and impl runs
(54 and 55 planned tests) waited on those pairs. The pairs had
opened in none of the run's six fills.

The plan's sentence: every run with a planned test the keeps did not move
stands; a run whose every planned test moved is refused. Negative control:
the any-overlap rule (`moved_in_scope` non-empty) refuses every run of the
fill, the live outcome.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    _GroundGate,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_outcome import (
    PremiseEntry,
    ground_left,
    moved_in_scope,
)

_FIXTURE = (Path(__file__).parent / "fixtures" / "premise"
            / "116341-24c76a97_fill6.json")


def _rec() -> dict:
    return json.loads(_FIXTURE.read_text())


def _read(rec: dict) -> set[str]:
    """Every test the fill's plan read: the planned tests and the moved
    ones (all were failing tests of the sweep)."""
    return set(rec["moved"]).union(
        *(set(r["target_tests"]) for r in rec["runs"].values()))


def test_every_run_with_an_unmoved_planned_test_stands() -> None:
    rec = _rec()
    moved, read = rec["moved"], _read(rec)
    for label, run in rec["runs"].items():
        scope = frozenset(run["target_tests"])
        left = ground_left(moved, scope, read)
        whole_moved = scope <= set(moved)
        assert bool(left) is not whole_moved, label


def test_the_formatter_pairs_and_the_error_rs_runs_stand_on_52_tests() -> None:
    rec = _rec()
    moved, read = rec["moved"], _read(rec)
    for label in ("pair src/error.rs::fmt + src/fmt.rs::debug",
                  "pair src/error.rs::fmt + src/fmt.rs::display",
                  "file src/error.rs [4 unit(s) to try]",
                  "impl src/error.rs [2 unit(s) to try]"):
        scope = frozenset(rec["runs"][label]["target_tests"])
        assert len(ground_left(moved, scope, read)) == 52, label


def test_a_run_whose_only_planned_test_moved_is_still_refused() -> None:
    rec = _rec()
    moved, read = rec["moved"], _read(rec)
    for label in ("impl src/ptr.rs", "file src/ptr.rs [1 not landed this fill]"):
        scope = frozenset(rec["runs"][label]["target_tests"])
        assert ground_left(moved, scope, read) == (), label


def test_negative_control_any_overlap_refuses_every_run_of_the_fill() -> None:
    rec = _rec()
    moved = rec["moved"]
    refused = [label for label, run in rec["runs"].items()
               if moved_in_scope(moved, frozenset(run["target_tests"]))]
    assert refused == list(rec["runs"])


def test_a_planned_test_the_plan_never_read_is_not_ground() -> None:
    assert ground_left(["t1"], {"t1", "t9"}, {"t1"}) == ()
    assert ground_left(["t1"], {"t1", "t2"}, {"t1", "t2"}) == ("t2",)
    assert ground_left(["t1"], None, {"t1", "t2"}) == ()


def _gate(plan: dict[str, PremiseEntry],
          now: dict[str, PremiseEntry]) -> _GroundGate:
    gate = _GroundGate(FillExecutionDeps(
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        check_stop=lambda _c: None, ledger=None, project="p", cluster="c",
        run_id="r", iteration=6, oracle_fp="o",
        premise=plan, ground_now=lambda: now))
    fill_reading.note_kept(0)
    return gate


def test_the_gate_refuses_only_the_run_whose_whole_ground_moved() -> None:
    """The gate on the recorded shape: a planned test set of which a keep
    moved two stands; a one-test run whose test moved is refused."""
    rec = _rec()
    wide = rec["runs"]["pair src/error.rs::fmt + src/fmt.rs::debug"]
    narrow = rec["runs"]["impl src/ptr.rs"]
    tests = set(wide["target_tests"]) | set(narrow["target_tests"]) | set(
        rec["moved"])
    plan: dict[str, PremiseEntry] = dict.fromkeys(tests, (False, ("fn", "src/error.rs", "context", ""), False))
    now = {**plan, **dict.fromkeys(rec["moved"], (False, ("fn", "src/ptr.rs", "drop", ""), False))}
    gate = _gate(plan, now)
    units = frozenset({FnLocator("src/fmt.rs", "debug")})
    assert not gate(frozenset(wide["target_tests"]), units)
    assert gate(frozenset(narrow["target_tests"]),
                frozenset({FnLocator("src/ptr.rs", "as_ptr")}))
