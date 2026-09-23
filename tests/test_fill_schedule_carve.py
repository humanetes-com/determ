"""A route that writes each target on its own is ranked by unit, not by batch.

The defect, run `3131-f6b37c1a` (2026-09-23, cluster `stable`, oracle
`1c3b06644b2e`, the tree restarted from the augmented tag): the per-fn
batch of 12 stubs was deferred `rebuy_flat` in fills 1, 2 and 3 because the
schedule ranks a run by its WORST target, and one target's last per-fn try
(`497151-a6fa205b`) had been flat. The other eleven's last per-fn try
(`713017-baa50600`) had paid, and among them was the stub 45 of the 150
failing tests first panicked in. No route opened any of them; the score sat
at 29/184.

The plan's sentence: a divisible run is split into the units that are a new
or paying try, which open, and the re-buy units, which are deferred as their
own run and accrue their own deferrals. The negative control is the same
run without ``unit_tests`` (a transaction), which reproduces the live
whole-batch deferral.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    UnitService,
    carve_rebuys,
    schedule,
)
from pipeline_core.domain.models import FnLocator

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "3131-f6b37c1a_fill1_per_fn.json")

_A, _B, _C, _D = (FnLocator("src/p.rs", n) for n in ("a", "b", "c", "d"))


def _row(targets: Iterable[FnLocator], outcome: RunOutcome, *, started: str,
         delta: int = 0, kind: RouteKind = RouteKind.PER_FN) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label=f"{kind.value} run", bodies_sha="old",
        targets=tuple(sorted((t.file, t.fn_name) for t in targets)),
        oracle_fp="o", outcome=outcome, net_pass_delta=delta,
        model_calls=1, minutes=5.0, started=started)


def _per_fn(targets: Iterable[FnLocator], *, divisible: bool) -> RouteRun:
    ts = sorted(targets, key=lambda u: (u.file, u.fn_name))
    return RouteRun(
        kind=RouteKind.PER_FN, label="per-fn batch", targets=frozenset(ts),
        target_tests=frozenset(f"t_{t.fn_name}" for t in ts),
        stubs_in_targets=frozenset(ts), bodies_sha="new",
        unit_tests=(tuple((t, frozenset({f"t_{t.fn_name}"})) for t in ts)
                    if divisible else ()))


def _ledger() -> list[RouteLedgerRow]:
    """a, b, c paid on their last per-fn try; d's last per-fn try was flat."""
    return [_row([_A, _B, _C, _D], RunOutcome.FLAT, started="2026-01-01T00:00"),
            _row([_A, _B, _C], RunOutcome.CASHED, delta=1,
                 started="2026-01-02T00:00")]


#: A never-served competitor, as every live fill has one: with a re-buy the
#: only candidate the schedule re-admits everything (its own rule).
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  target_tests=frozenset({"t_q"}),
                  bodies_sha="new")


def test_one_flat_unit_no_longer_defers_the_units_that_paid() -> None:
    run = _per_fn([_A, _B, _C, _D], divisible=True)
    sched = schedule([run, _OTHER], _ledger(), oracle_fp="o", hollow={},
                     budget_minutes=200)
    opened = [s for s in sched.ordered if s.run.kind is RouteKind.PER_FN]
    assert [frozenset(s.run.targets) for s in opened] == [frozenset({_A, _B, _C})]
    assert opened[0].reason == "paying"
    assert opened[0].run.parent == run.key
    assert opened[0].run.target_tests == frozenset({"t_a", "t_b", "t_c"})
    assert [(frozenset(s.run.targets), s.reason) for s in sched.deferred] \
        == [(frozenset({_D}), "rebuy_flat")]
    # Negative control: the same batch as one transaction is deferred whole.
    whole = schedule([_per_fn([_A, _B, _C, _D], divisible=False), _OTHER],
                     _ledger(), oracle_fp="o", hollow={}, budget_minutes=200)
    assert [s.run.label for s in whole.ordered] == ["file src/q.rs"]
    assert [s.reason for s in whole.deferred] == ["rebuy_flat"]


def test_an_all_rebuy_batch_and_a_transaction_are_not_split() -> None:
    rows = [_row([_A, _B], RunOutcome.FLAT, started="2026-01-01T00:00")]
    assert carve_rebuys(_per_fn([_A, _B], divisible=True), _unit_service(rows),
                        readmit_after=2) == (_per_fn([_A, _B], divisible=True),)
    t = _per_fn([_A, _B, _C, _D], divisible=False)
    assert carve_rebuys(t, _unit_service(_ledger()), readmit_after=2) == (t,)


def test_a_carved_rebuy_unit_is_readmitted_on_its_own_deferral_count() -> None:
    """The re-buy piece is rowed DEFERRED naming its units; after
    `readmit_after` of those, the unit rides with the open piece again."""
    rows = _ledger() + [
        _row([_D], RunOutcome.DEFERRED, started=f"2026-01-0{3 + i}T00:00")
        for i in range(2)]
    sched = schedule([_per_fn([_A, _B, _C, _D], divisible=True), _OTHER], rows,
                     oracle_fp="o", hollow={}, budget_minutes=200,
                     readmit_after=2)
    assert not sched.deferred
    assert {frozenset(s.run.targets) for s in sched.ordered
            if s.run.kind is RouteKind.PER_FN} == {frozenset({_A, _B, _C, _D})}


def test_carved_pieces_carry_the_executor_s_bodies_identity() -> None:
    sched = schedule([_per_fn([_A, _B, _C, _D], divisible=True), _OTHER],
                     _ledger(), oracle_fp="o", hollow={}, budget_minutes=200,
                     bodies_sha=lambda ts: f"sha{len(ts)}")
    assert [s.run.bodies_sha for s in sched.ordered
            if s.run.kind is RouteKind.PER_FN] == ["sha3"]
    assert sched.deferred[0].run.bodies_sha == "sha1"


def _unit_service(rows: list[RouteLedgerRow]) -> UnitService:
    return UnitService(rows, "o")


def test_recorded_3131_fill_1_opens_the_per_fn_units_that_paid() -> None:
    """Plan sentence, on the recorded fill: the per-fn run opens holding
    every target but `src/fmt.rs::display` (the one whose last per-fn try was
    flat) — `src/error.rs::msg` and `src/lib.rs::format_err` among them —
    and only `display` is deferred."""
    fix = json.loads(FIXTURE.read_text())
    rec = fix["per_fn_run"]
    rows = [r for r in (RouteLedgerRow.from_dict(x) for x in fix["ledger"])
            if r is not None]

    def loc(s: str) -> FnLocator:
        f, fn = s.split("::", 1)
        return FnLocator(f, fn)

    targets = sorted((loc(x) for x in rec["targets"]),
                     key=lambda u: (u.file, u.fn_name))
    tests = frozenset(rec["target_tests"])

    def run(divisible: bool) -> RouteRun:
        return RouteRun(
            kind=RouteKind.PER_FN, label=rec["label"],
            targets=frozenset(targets), target_tests=tests,
            stubs_in_targets=frozenset(loc(x) for x in rec["stubs"]),
            files=tuple(rec["files"]), seq=rec["seq"],
            bodies_sha=rec["bodies_sha"],
            unit_tests=(tuple((t, tests) for t in targets)
                        if divisible else ()))

    kw = {"oracle_fp": fix["oracle_fp"], "hollow": {},
          "budget_minutes": fix["budget_minutes"], "readmit_after": 2}
    sched = schedule([run(True), _OTHER], rows, **kw)
    opened = {t.file + "::" + t.fn_name for s in sched.ordered
              if s.run.kind is RouteKind.PER_FN for t in s.run.targets}
    deferred = {t.file + "::" + t.fn_name for s in sched.deferred
                for t in s.run.targets}
    assert deferred == {"src/fmt.rs::display"}
    assert {"src/error.rs::msg", "src/lib.rs::format_err"} <= opened
    assert len(opened) == 11
    # Negative control: the whole batch, as the live schedule ranked it.
    whole = schedule([run(False), _OTHER], rows, **kw)
    assert [s.run.label for s in whole.ordered] == ["file src/q.rs"]
    assert [s.reason for s in whole.deferred] == [fix["live_reason"]] \
        == ["rebuy_flat"]
