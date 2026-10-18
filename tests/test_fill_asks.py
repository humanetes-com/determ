"""Within one fill, a run whose every target was already asked under
evidence no later run moved is counted (shadow) as `asked_this_fill`.

Run `3076-051d385a` iter 2 (2026-10-13): one Debug implementer was the
target of five runs of one fill — a pair reverted on 8 newly red (new
evidence), a file run kept flat, an empty impl run, a session with no
change, and a per-fn run (39.3 min) that repeated its error set. The count
names the per-fn run; no run is deferred on it until the route order has
its own reading.

Asserted on an invented shape: three routes list one unit; the first
reverts with no newly red test and the second keeps flat, so the third
would be deferred. Variant: a first revert that names newly red tests lets
the second route ask, and a cash clears the set.
"""
from __future__ import annotations

from pipeline_core.domain.fill_asks import FillAsks
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RunOutcome,
)

_U = ("src/k.rs", "show")


def _row(outcome: RunOutcome, **kw) -> RouteLedgerRow:
    return RouteLedgerRow(kind=RouteKind.FILE, label="r", targets=(_U,),
                          bodies_sha="", oracle_fp="o", outcome=outcome, **kw)


def test_a_third_route_on_unmoved_evidence_would_be_deferred() -> None:
    asks = FillAsks()
    assert not asks.would_defer([_U])
    asks.note(_row(RunOutcome.REVERTED), [_U])
    asks.note(_row(RunOutcome.FLAT), [_U])
    assert asks.would_defer([_U])


def test_new_evidence_reopens_the_ask() -> None:
    asks = FillAsks()
    asks.note(_row(RunOutcome.REVERTED, newly_red=("t_sibling",)), [_U])
    assert not asks.would_defer([_U])
    asks.note(_row(RunOutcome.FLAT), [_U])
    assert asks.would_defer([_U])
    asks.note(_row(RunOutcome.CASHED, net_pass_delta=1,
                   newly_green=("t_red",)), [_U])
    assert not asks.would_defer([_U])


def test_a_run_that_asked_nothing_records_nothing() -> None:
    asks = FillAsks()
    for oc in (RunOutcome.DEFERRED, RunOutcome.EMPTY, RunOutcome.STOPPED):
        asks.note(_row(oc), [_U])
    assert not asks.would_defer([_U])


def test_a_compile_revert_of_a_joint_write_leaves_its_units_open() -> None:
    """A file reply reverted on a compile error judged the joint write; the
    per-block fallback is the narrower ask it exists for. Control: a revert
    with no compile class (a crash, a wrong body) records the ask."""
    asks = FillAsks()
    asks.note(_row(RunOutcome.REVERTED, classes={"compile": 1}), [_U])
    assert not asks.would_defer([_U])
    asks.note(_row(RunOutcome.REVERTED, classes={"wrong": 1}), [_U])
    assert asks.would_defer([_U])


def test_a_reply_with_no_body_is_not_an_ask() -> None:
    """A file call that replied with no FN tags (an answer dropped) judged
    nothing; its blocks keep the per-block fallback. Control: a flat keep
    is an ask."""
    asks = FillAsks()
    asks.note(_row(RunOutcome.FLAT, classes={"dropped": 1}), [_U])
    assert not asks.would_defer([_U])
    asks.note(_row(RunOutcome.FLAT, kept=(_U,)), [_U])
    assert asks.would_defer([_U])
