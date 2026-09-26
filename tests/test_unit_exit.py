"""One typed exit per dispatched unit per fill.

The class, from run `38934-2999cc94` (2026-09-26): the landed-change audit said
`no route attempted` for units of the batch on fills 5 and 6, and the route
ledger said something else about the same units — two accountings, no way to
tell a legitimate deferral from a silent drop. Over 80 runs since 2026-08-31
(`scripts/stage_leak_census.py`) 29% of dispatched units were unattempted, and
of those joined to the fill's ledger 37% sat in an executed run's targets, 46%
only in deferred runs' targets and 17% in no run's targets.

The plan's sentences, each with its control:
- a unit the audit calls unattempted is classified by the fill's own rows:
  an executed run holding it, a deferred run holding it (with the reasons),
  or no run at all — and the three exits are EXACTLY the audit's unattempted
  set (the classification adds no unit and drops none);
- on the recorded fills 5 and 6 the two units of the recursion are held by no
  run, and the units the per-fn run targeted are not;
- an empty row set reads every unattempted unit as held by no run, which is why
  the loop skips the classification when the ledger returned nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RunOutcome,
)
from pipeline_core.domain.landed_change import unattempted_units
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.unit_exit import (
    ExitKind,
    exit_histogram,
    exit_line,
    exits_payload,
    unit_exits,
)

_FIX = Path(__file__).parent / "fixtures" / "route_ledger"


def _loc(s: str) -> FnLocator:
    f, n = s.rsplit("::", 1)
    return FnLocator(f, n)


def _row(kind: str, label: str, targets: list[str], outcome: str,
         reason: str = "") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=RouteKind(kind), label=label,
        targets=tuple((t.rsplit("::", 1)[0], t.rsplit("::", 1)[1])
                      for t in targets),
        bodies_sha="x", oracle_fp="o", outcome=RunOutcome(outcome),
        reason=reason)


def test_each_unattempted_unit_is_classified_by_the_fills_own_rows() -> None:
    rows = [_row("file", "file src/a.rs", ["src/a.rs::run"], "flat"),
            _row("pair", "pair a+b", ["src/a.rs::wait"], "deferred", "pair_wait"),
            _row("file", "file src/c.rs", ["src/a.rs::wait"], "deferred",
                 "premise_moved")]
    batch = [_loc(u) for u in ("src/a.rs::landed", "src/a.rs::tried",
                               "src/a.rs::run", "src/a.rs::wait",
                               "src/a.rs::lost")]
    ex = unit_exits(batch, attempted={_loc("src/a.rs::tried")},
                    landed={_loc("src/a.rs::landed")}, rows=rows)
    assert ex[_loc("src/a.rs::landed")].kind is ExitKind.LANDED
    assert ex[_loc("src/a.rs::tried")].kind is ExitKind.ATTEMPTED
    assert ex[_loc("src/a.rs::run")].kind is ExitKind.RUN_TARGET
    assert ex[_loc("src/a.rs::wait")].kind is ExitKind.DEFERRED
    assert ex[_loc("src/a.rs::wait")].detail == "pair_wait, premise_moved"
    assert ex[_loc("src/a.rs::lost")].kind is ExitKind.UNROUTED
    # No unit added, none dropped: the non-attempted exits ARE the audit's set.
    left = {u for u, e in ex.items() if e.kind in (
        ExitKind.RUN_TARGET, ExitKind.DEFERRED, ExitKind.UNROUTED)}
    assert left == set(unattempted_units(
        batch, {_loc("src/a.rs::tried")}, {_loc("src/a.rs::landed")}))


def test_negative_control_no_rows_reads_every_unattempted_unit_as_unrouted() -> None:
    batch = [_loc("src/a.rs::u"), _loc("src/a.rs::v")]
    ex = unit_exits(batch, attempted={_loc("src/a.rs::u")}, landed=set(), rows=[])
    assert ex[_loc("src/a.rs::v")].kind is ExitKind.UNROUTED   # the false reading


def _fill(n: str):
    fx = json.loads((_FIX / "38934-2999cc94_fills5_6.json").read_text())
    f = fx["fills"][n]
    rows = [_row(r["kind"], r["label"],
                 [f"{a}::{b}" for a, b in r["targets"]], r["outcome"],
                 r["reason"] if r["outcome"] == "deferred" else "")
            for r in f["rows"]]
    att = {_loc(u) for u in f["attempted"]}
    land = {_loc(u) for u in f["landed"]}
    unatt = {_loc(u) for u in f["unattempted"]}
    return rows, att, land, unatt


def test_the_recorded_recursion_units_are_held_by_no_run_on_fills_5_and_6() -> None:
    for n, lost in (("5", {"src/backtrace.rs::from_boxed",
                           "src/error.rs::construct_from_boxed"}),
                    ("6", {"src/backtrace.rs::new",
                           "src/error.rs::construct_from_boxed"})):
        rows, att, land, unatt = _fill(n)
        ex = unit_exits(att | land | unatt, attempted=att, landed=land,
                        rows=rows)
        assert {u for u, e in ex.items() if e.kind is ExitKind.UNROUTED} == {
            _loc(u) for u in lost}, n
        left = {u for u, e in ex.items() if e.kind in (
            ExitKind.RUN_TARGET, ExitKind.DEFERRED, ExitKind.UNROUTED)}
        assert left == unatt, n
    # Fill 6's other two unattempted units were targets of the per-fn run.
    rows, att, land, unatt = _fill("6")
    ex = unit_exits(att | land | unatt, attempted=att, landed=land, rows=rows)
    assert ex[_loc("src/error.rs::deref")].kind is ExitKind.RUN_TARGET


def test_the_line_names_the_histogram_the_reasons_and_the_lost_units() -> None:
    rows, att, land, unatt = _fill("6")
    ex = unit_exits(att | land | unatt, attempted=att, landed=land, rows=rows)
    line = exit_line(ex)
    assert line.startswith("exits: ") and "unrouted 2" in line
    assert "held by NO run: src/backtrace.rs::new, src/error.rs::construct_from_boxed" in line
    payload = exits_payload(ex)
    assert payload["histogram"]["unrouted"] == 2   # type: ignore[index]
    assert "src/error.rs::construct_from_boxed" in payload["left"]  # type: ignore[operator]
    assert exit_histogram(ex)[ExitKind.UNROUTED] == 2
