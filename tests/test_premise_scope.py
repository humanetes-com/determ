"""A keep refuses only the claims planned on the tests it moved.

Recorded fixture: run `497151-a6fa205b` (2026-09-22, cluster `stable`,
iter 1). The sweep's stub holds (each failing test held by the still-stubbed
fn it first fails in) and the 12-unit per-fn batch. The first keep of fill 1
was `src/error.rs::new`; the gate saw exactly its 13 held tests move (to
`construct_from_std`) and, fill-wide, refused the 8 unclaimed units, none of
whose 55 held tests had moved. Three iterations in a row landed only the
four bodies in flight.

The plan's sentence: after that keep, every unit whose held tests did not
move is still claimed; only a claim planned on a moved test is refused.
Negative control: the same claims asked without their own tests (the run's
whole scope, which contains the moved tests) are refused after the keep —
the live outcome.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_outcome import moved_in_scope

_FIXTURE = (Path(__file__).parent / "fixtures" / "premise"
            / "497151-a6fa205b_iter1_holds.json")


def _loc(key: str) -> FnLocator:
    file, _, fn = key.partition("::")
    return FnLocator(file, fn)


def _site(key: str) -> tuple[str, ...]:
    file, _, fn = key.partition("::")
    return ("fn", file, fn, "")


class _PooledRoute:
    """Claims the batch one unit at a time, asking with the unit's own
    tests (or none, for the control); the recorded first keep lands."""

    kind = RouteKind.PER_FN
    parallel = False

    def __init__(self, run: RouteRun, batch: Sequence[str],
                 held: dict[str, list[str]], keep: str, *,
                 own_tests: bool) -> None:
        self._run = run
        self._batch = batch
        self._held = held
        self._keep = keep
        self._own = own_tests
        self.claimed: list[str] = []

    def plan(self) -> Sequence[RouteRun]:
        return [self._run]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        for unit in self._batch:
            tests = frozenset(self._held.get(unit, ())) if self._own else None
            if not clock.may_claim(tests):
                continue
            self.claimed.append(unit)
            if unit == self._keep:
                fill_reading.note_kept(0)
        return RouteRunReport(model_calls=len(self.claimed))


def _replay(tmp_path: Path, *, own_tests: bool,
            shared: tuple[str, str] | None = None) -> tuple[list[str], tuple]:
    """``shared`` = (unit, test): that unit's planned tests also carry one
    of the kept unit's held tests, so the keep moves its ground too."""
    rec = json.loads(_FIXTURE.read_text())
    held: dict[str, list[str]] = {u: list(ts) for u, ts in rec["held"].items()}
    batch: list[str] = rec["batch"]
    keep: str = rec["first_keep"]
    plan = {t: (False, _site(u), False) for u, ts in held.items() for t in ts}
    if shared is not None:
        held[shared[0]].append(shared[1])
    # After the keep: the kept unit's held tests first fail one link down.
    after = dict(plan)
    for t in held[keep]:
        after[t] = (False, ("fn", "src/error.rs", "construct_from_std", ""),
                    False)
    run = RouteRun(
        kind=RouteKind.PER_FN, label=f"per-fn {len(batch)} stub(s)",
        targets=frozenset(_loc(u) for u in batch),
        target_tests=frozenset(t for u in batch for t in held.get(u, ())),
        stubs_in_targets=frozenset(_loc(u) for u in batch),
        files=tuple(sorted({_loc(u).file for u in batch})))
    route = _PooledRoute(run, batch, held, keep, own_tests=own_tests)
    fill_reading.reset_last_comparison()
    out = execute_fill(
        [route], hollow={}, bodies_sha=lambda _t: "b",
        deps=FillExecutionDeps(
            log=lambda _m: None, emit=lambda *_a, **_k: None,
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=1,
            oracle_fp="o", premise=plan, ground_now=lambda: after))
    return route.claimed, out.premise_moved


def test_every_unit_whose_held_tests_did_not_move_is_still_claimed(
        tmp_path: Path) -> None:
    rec = json.loads(_FIXTURE.read_text())
    claimed, moved = _replay(tmp_path, own_tests=True)
    assert set(moved) == set(rec["held"][rec["first_keep"]])
    assert len(moved) == 13
    assert claimed == rec["batch"]


def test_a_later_unit_planned_on_a_moved_test_is_refused_beside_ones_that_are_not(
        tmp_path: Path) -> None:
    """The recorded holds partition the tests, so no other unit of the batch
    shares one of `new`'s. Give one later unit one of them: it is refused;
    every unit before and after it that shares none is still claimed. A
    gate that never refuses a scoped claim fails here."""
    rec = json.loads(_FIXTURE.read_text())
    batch: list[str] = rec["batch"]
    keep = rec["first_keep"]
    later = batch[batch.index(keep) + 2]
    moved_test = rec["held"][keep][0]
    claimed, moved = _replay(tmp_path, own_tests=True,
                             shared=(later, moved_test))
    assert moved_test in moved
    assert later not in claimed
    assert claimed == [u for u in batch if u != later]


def test_negative_control_the_run_wide_claim_refuses_the_rest_of_the_batch(
        tmp_path: Path) -> None:
    rec = json.loads(_FIXTURE.read_text())
    claimed, _moved = _replay(tmp_path, own_tests=False)
    batch = rec["batch"]
    assert claimed == batch[:batch.index(rec["first_keep"]) + 1]


def test_scope_matches_across_the_spellings_readers_carry() -> None:
    moved = ["test_a", "mod::gen_tests_3::test_b",
             "src/lib.rs - Thing::go#0"]
    assert moved_in_scope(moved, {"test_b"}) == ("mod::gen_tests_3::test_b",)
    assert moved_in_scope(moved, {"src/lib.rs - Thing::go (line 12)"}) == (
        "src/lib.rs - Thing::go#0",)
    assert moved_in_scope(moved, {"test_c"}) == ()


def test_a_claim_with_no_named_test_stands_on_the_whole_ground() -> None:
    assert moved_in_scope(["t1"], None) == ("t1",)
    assert moved_in_scope(["t1"], frozenset()) == ("t1",)
    assert moved_in_scope([], None) == ()
