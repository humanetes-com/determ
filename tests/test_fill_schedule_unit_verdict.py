"""A unit is convicted by its own graded body, not by the run it rode in.

The defect, run `3131-f6b37c1a` (2026-09-23, cluster `stable`, oracle
`1c3b06644b2e`): the fill schedule read a route row's run-level outcome as
a verdict on every target of the row. Of the five per-fn convictions that
deferred the batch holding `src/error.rs::msg` (45 of 150 failing tests
first panic in it), one was a graded body that ran its tests and stayed red
— `src/fmt.rs::display` was convicted by a row that never kept it and whose
only class was `unreachable`. Under that oracle 43 of 86 FLAT route rows
carried only `unreachable`.

The plan's sentences, each with its negative control (the route-row reading,
``unit_evidence=None``):

- a target a FLAT row never kept is not a try;
- a landing the sweep graded unmeasured (tests did not run) is no verdict;
- a landing graded flat convicts only while that body is on the tree with
  its premise standing; off the tree, or under moved evidence, it does not;
- a reverted unit is a re-buy;
- a landing graded advanced pays; a row that cashed on a sibling does not
  make a unit graded flat pay;
- the grade is found by run id and body sha, whatever iteration the sweep
  stamped;
- an identical run's FLAT row does not bypass the per-unit verdict;
- on the recorded 3131 fills, the per-fn run opens with `msg` in all three.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    UnitEvidence,
    schedule,
    unit_evidence_from,
)
from pipeline_core.domain.models import FnLocator

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "3131-f6b37c1a_unit_verdict.json")

_A, _B = FnLocator("src/p.rs", "a"), FnLocator("src/p.rs", "b")
#: A never-served competitor: with a re-buy as the only candidate the
#: schedule re-admits everything (its own rule), which would hide a verdict.
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  bodies_sha="new")


def _pairs(units: Iterable[FnLocator]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((u.file, u.fn_name) for u in units))


def _row(targets: Iterable[FnLocator], outcome: RunOutcome, *,
         kept: Iterable[FnLocator] = (), reverted: Iterable[FnLocator] = (),
         delta: int = 0, run_id: str = "r1", iteration: int = 1,
         kind: RouteKind = RouteKind.PER_FN, bodies: str = "old",
         started: str = "2026-01-01T00:00") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label=f"{kind.value} run", bodies_sha=bodies,
        targets=_pairs(targets), oracle_fp="o", outcome=outcome,
        kept=_pairs(kept), reverted=_pairs(reverted), net_pass_delta=delta,
        model_calls=1, minutes=5.0, run_id=run_id, iteration=iteration,
        started=started)


def _per_fn(targets: Iterable[FnLocator], *, bodies: str = "new",
            divisible: bool = True) -> RouteRun:
    ts = sorted(targets, key=lambda u: (u.file, u.fn_name))
    return RouteRun(
        kind=RouteKind.PER_FN, label="per-fn batch", targets=frozenset(ts),
        target_tests=frozenset(f"t_{u.fn_name}" for u in ts),
        stubs_in_targets=frozenset(ts), bodies_sha=bodies,
        unit_tests=(tuple((u, frozenset({f"t_{u.fn_name}"})) for u in ts)
                    if divisible else ()))


def _ev(grades: dict[FnLocator, tuple[str, str | None]], *,
        standing: dict[FnLocator, str] | None = None,
        run_id: str = "r1", iteration: int = 1) -> UnitEvidence:
    return UnitEvidence(
        grades={(run_id, iteration, u): g for u, g in grades.items()},
        standing_flat=standing or {})


def _deferred_units(rows: list[RouteLedgerRow], run: RouteRun,
                    ev: UnitEvidence | None) -> frozenset[FnLocator]:
    sched = schedule([run, _OTHER], rows, oracle_fp="o", hollow={},
                     budget_minutes=200, unit_evidence=ev)
    return frozenset(t for s in sched.deferred for t in s.run.targets)


def test_a_target_the_flat_row_never_kept_is_not_convicted() -> None:
    rows = [_row([_A, _B], RunOutcome.FLAT, kept=[_A])]
    ev = _ev({_A: ("sa", "flat")}, standing={_A: "sa"})
    assert _deferred_units(rows, _per_fn([_A, _B]), ev) == {_A}
    # Control, the route-row reading: the row convicts both.
    assert _deferred_units(rows, _per_fn([_A, _B]), None) == {_A, _B}


def test_an_unmeasured_landing_is_no_verdict() -> None:
    rows = [_row([_A], RunOutcome.FLAT, kept=[_A])]
    assert _deferred_units(rows, _per_fn([_A]),
                           _ev({_A: ("sa", "unmeasured")})) == frozenset()
    assert _deferred_units(rows, _per_fn([_A]), None) == {_A}


def test_a_flat_grade_convicts_only_while_its_body_stands_on_the_tree() -> None:
    rows = [_row([_A], RunOutcome.FLAT, kept=[_A])]
    on_tree = _ev({_A: ("sa", "flat")}, standing={_A: "sa"})
    assert _deferred_units(rows, _per_fn([_A]), on_tree) == {_A}
    # Off the tree (a redo re-stubbed it) or under moved evidence: the
    # driver's standing set does not hold it, so the claim is not asked.
    off_tree = _ev({_A: ("sa", "flat")}, standing={})
    assert _deferred_units(rows, _per_fn([_A]), off_tree) == frozenset()
    # Another body stands flat: not the one this route landed.
    other_body = _ev({_A: ("sa", "flat")}, standing={_A: "sz"})
    assert _deferred_units(rows, _per_fn([_A]), other_body) == frozenset()


def test_a_reverted_unit_is_a_rebuy() -> None:
    rows = [_row([_A, _B], RunOutcome.REVERTED, reverted=[_A])]
    sched = schedule([_per_fn([_A, _B]), _OTHER], rows, oracle_fp="o",
                     hollow={}, budget_minutes=200, unit_evidence=_ev({}))
    assert {(frozenset(s.run.targets), s.reason) for s in sched.deferred} \
        == {(frozenset({_A}), "rebuy_reverted")}


def test_paying_is_the_unit_s_grade_not_the_row_s_cash() -> None:
    rows = [_row([_A, _B], RunOutcome.CASHED, kept=[_A, _B], delta=1)]
    ev = _ev({_A: ("sa", "advanced"), _B: ("sb", "flat")})
    sched = schedule([_per_fn([_A]), _per_fn([_B])], rows, oracle_fp="o",
                     hollow={}, unit_evidence=ev)
    by = {frozenset(s.run.targets): s.reason for s in sched.ordered}
    assert by[frozenset({_A})] == "paying"
    # B rode in the cashed row but its own body was graded flat, and that
    # body is not standing: a new try, not a paying one.
    assert by[frozenset({_B})] == "served"
    control = schedule([_per_fn([_B])], rows, oracle_fp="o", hollow={})
    assert control.ordered[0].reason == "paying"


def test_the_grade_is_joined_by_run_and_body_whatever_iteration_the_sweep_stamped() -> None:
    def e(state: str, it: int, sha: str) -> SimpleNamespace:
        return SimpleNamespace(
            key=SimpleNamespace(file="src/p.rs", fn_name="a"), run_id="r1",
            iteration=it, body_sha=sha, state=state, oracle_fp="o")
    later = unit_evidence_from([e("landed", 3, "s1"), e("flat", 4, "s1")],
                               {}, oracle_fp="o")
    assert later.grade("r1", 3, _A) == ("s1", "flat")
    # A later process judged it and stamped an EARLIER iteration.
    earlier = unit_evidence_from([e("landed", 3, "s1"), e("advanced", 1, "s1")],
                                 {}, oracle_fp="o")
    assert earlier.grade("r1", 3, _A) == ("s1", "advanced")
    # Another body's grade is not this landing's.
    other = unit_evidence_from([e("landed", 3, "s1"), e("flat", 4, "s2")],
                               {}, oracle_fp="o")
    assert other.grade("r1", 3, _A) == ("s1", None)
    # Under another oracle: nothing.
    assert unit_evidence_from([e("landed", 3, "s1")], {},
                              oracle_fp="x").grade("r1", 3, _A) is None


def test_an_identical_run_s_flat_row_does_not_bypass_the_unit_verdict() -> None:
    """Same targets, same bodies sha: the identity path used to rank from
    the row's FLAT and never ask the units."""
    rows = [_row([_A, _B], RunOutcome.FLAT, kept=[_A, _B], bodies="same")]
    ev = _ev({_A: ("sa", "unmeasured"), _B: ("sb", "unmeasured")})
    for divisible in (True, False):
        run = _per_fn([_A, _B], bodies="same", divisible=divisible)
        assert _deferred_units(rows, run, ev) == frozenset()
        assert _deferred_units(rows, run, None) == {_A, _B}


def test_a_session_is_still_ranked_by_its_rows() -> None:
    """A session's try is keyed by the tests it aims at; the units its
    transcript touched are not the question."""
    rows = [_row([_A], RunOutcome.FLAT, kept=[_A], kind=RouteKind.SESSION)]
    run = RouteRun(kind=RouteKind.SESSION, label="session", bodies_sha="new",
                   targets=frozenset({_A}))
    sched = schedule([run, _OTHER], rows, oracle_fp="o", hollow={},
                     budget_minutes=200,
                     unit_evidence=_ev({_A: ("sa", "unmeasured")}))
    assert [s.reason for s in sched.deferred] == ["rebuy_flat"]


def _loc(s: str) -> FnLocator:
    f, fn = s.split("::", 1)
    return FnLocator(f, fn)


def test_recorded_3131_fills_open_the_per_fn_run_holding_msg() -> None:
    """Plan sentence, on the recorded fills with the real body ledger and
    the dispatch step's recorded standing-FLAT set (empty on every
    iteration of the run): each fill opens a per-fn run holding
    `src/error.rs::msg`; the route-row reading defers the whole batch, as
    live."""
    fix = json.loads(FIXTURE.read_text())
    rows = [r for r in (RouteLedgerRow.from_dict(x) for x in fix["ledger"])
            if r is not None]
    body = [SimpleNamespace(key=SimpleNamespace(file=b["file"], fn_name=b["fn"]),
                            run_id=b["run_id"], iteration=b["iteration"],
                            body_sha=b["body_sha"], state=b["state"],
                            oracle_fp=b["oracle_fp"])
            for b in fix["body_ledger"]]
    ev = unit_evidence_from(
        body, {_loc(k): v for k, v in fix["standing_flat"].items()},
        oracle_fp=fix["oracle_fp"])
    msg = FnLocator("src/error.rs", "msg")
    for fill in fix["fills"]:
        rec = fill["per_fn_run"]
        targets = sorted((_loc(x) for x in rec["targets"]),
                         key=lambda u: (u.file, u.fn_name))
        tests = frozenset(rec["target_tests"])
        run = RouteRun(
            kind=RouteKind.PER_FN, label=rec["label"],
            targets=frozenset(targets), target_tests=tests,
            stubs_in_targets=frozenset(_loc(x) for x in rec["stubs"]),
            files=tuple(rec["files"]), seq=rec["seq"],
            bodies_sha=rec["bodies_sha"],
            unit_tests=tuple((t, tests) for t in targets))
        prior = [r for r in rows if r.started[:19] < fill["ts"][:19]]
        kw = {"oracle_fp": fix["oracle_fp"], "hollow": {},
              "budget_minutes": fill["budget_minutes"], "readmit_after": 2}
        sched = schedule([run, _OTHER], prior, unit_evidence=ev, **kw)
        opened = {t for s in sched.ordered if s.run.kind is RouteKind.PER_FN
                  for t in s.run.targets}
        assert msg in opened, fill["iter"]
        # Control: the route-row reading of the same batch, not divided.
        whole = replace(run, unit_tests=())
        control = schedule([whole, _OTHER], prior, **kw)
        assert [s.reason for s in control.deferred
                if s.run.kind is RouteKind.PER_FN] == [fill["live_reason"]] \
            == ["rebuy_flat"]
