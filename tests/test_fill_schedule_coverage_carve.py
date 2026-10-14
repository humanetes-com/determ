"""A coverage-split hold defers the extras, not the attributed units the run carries.

The defect, run `673664-014fd53a` (2026-10-14, cluster `stable`, a
from-scratch climb through task0c): five constructor stubs were the first
failure of 92 of 97 red tests. In fills 2, 3 and 4 the per-fn run that held
them also held one or three units in `src/lib.rs` / `src/ptr.rs`, files no
attributed member occupies, so `_targets_split` marked the whole run a
coverage split and the schedule deferred it `coverage_split` behind the
error.rs impl and file runs, which reverted those constructors on compile
each fill. When the grace lapsed in fill 5 the per-fn run kept two of them
within minutes.

The plan's sentences, asserted on the recorded fills:
- the run is carved by the split model's own rule: the attributed members
  together with their same-file extras (the file's installable complement)
  open as an attributed write; the extras in other files stay deferred
  `coverage_split`;
- fill 3's opened piece carries the four error.rs table stubs beside the
  five constructors (they are what a constructor's vtable installs);
- under the pre-change schedule the whole per-fn run is deferred
  `coverage_split` in all three fills, which is what the live run printed.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain import fill_schedule as fs
from pipeline_core.domain.body_ledger import LedgerEntry
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    UnitService,
    carve_coverage_split,
    carve_rebuys,
    schedule,
    unit_evidence_from,
)
from pipeline_core.domain.models import FnLocator

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "673664-014fd53a_fills2to4.json")

_A, _B = FnLocator("src/x.rs", "a"), FnLocator("src/x.rs", "b")
_TABLE = FnLocator("src/x.rs", "table")
_C = FnLocator("src/y.rs", "c")


def _per_fn(targets: set[FnLocator], tests: set[str], *,
            divisible: bool = True) -> RouteRun:
    ts = sorted(targets, key=lambda u: (u.file, u.fn_name))
    return RouteRun(
        kind=RouteKind.PER_FN, label="per-fn batch", targets=frozenset(ts),
        target_tests=frozenset(tests), stubs_in_targets=frozenset(ts),
        files=tuple(sorted({t.file for t in ts})), bodies_sha="p",
        unit_tests=(tuple((t, frozenset(tests)) for t in ts)
                    if divisible else ()))


def _holder() -> RouteRun:
    return RouteRun(kind=RouteKind.IMPL, label="impl src/x.rs",
                    targets=frozenset({_A, _B}), target_tests=frozenset({"t"}),
                    stubs_in_targets=frozenset({_A, _B}), files=("src/x.rs",),
                    bodies_sha="i")


def test_the_split_is_carved_by_file_not_by_attribution() -> None:
    """A, B attributed in x.rs; `table` an unattributed x.rs unit; C in y.rs.
    The first piece is {A, B, table}, the second {C}."""
    run = _per_fn({_A, _B, _TABLE, _C}, {"t"})
    first, second = carve_coverage_split(run, {_A, _B})
    assert first.targets == {_A, _B, _TABLE}
    assert second.targets == {_C}
    assert first.parent == second.parent == run.plan_key
    assert second.target_tests == run.target_tests
    assert dict(first.unit_tests).keys() == {_A, _B, _TABLE}


def test_not_a_split_a_transaction_or_no_attributed_member_stays_whole() -> None:
    same_file = _per_fn({_A, _TABLE}, {"t"})
    assert carve_coverage_split(same_file, {_A}) == (same_file,)
    transaction = _per_fn({_A, _C}, {"t"}, divisible=False)
    assert carve_coverage_split(transaction, {_A}) == (transaction,)
    coverage_only = _per_fn({_C}, {"t"})
    assert carve_coverage_split(coverage_only, {_A}) == (coverage_only,)
    assert carve_coverage_split(_per_fn({_A, _C}, {"t"}), ()) == (
        _per_fn({_A, _C}, {"t"}),)


def test_the_attributed_part_opens_and_the_extras_wait_behind_the_holder() -> None:
    """Shape, no live names: the holder writes A, B; the per-fn run holds
    A, B and C in another file. The A, B part opens; C waits `coverage_split`
    behind the holder. Negative control: the pre-change schedule defers the
    whole per-fn run."""
    runs = [_holder(), _per_fn({_A, _B, _C}, {"t"})]
    sched = schedule(runs, [], oracle_fp="o", hollow={}, attributed={_A, _B},
                     budget_minutes=60)
    opened = [s.run for s in sched.ordered if s.run.kind is RouteKind.PER_FN]
    assert [r.targets for r in opened] == [frozenset({_A, _B})]
    assert [(s.run.targets, s.reason) for s in sched.deferred] == [
        (frozenset({_C}), "coverage_split")]
    assert sched.deferred[0].held_by

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "carve_coverage_split", lambda r, a: (r,))
        before = schedule(runs, [], oracle_fp="o", hollow={},
                          attributed={_A, _B}, budget_minutes=60)
    assert not [s for s in before.ordered if s.run.kind is RouteKind.PER_FN]
    assert [s.reason for s in before.deferred] == ["coverage_split"]


def test_a_rebuy_carve_of_a_split_piece_keeps_the_planned_parent() -> None:
    """The executor resolves a run by `plan_key`. A re-buy carve applied to
    a split piece must name the planned run, not the piece."""
    run = _per_fn({_A, _B, _C}, {"t"})
    first, _ = carve_coverage_split(run, {_A, _B})
    rows = [RouteLedgerRow(kind=RouteKind.PER_FN, label="per-fn",
                           bodies_sha="old", targets=(("src/x.rs", "a"),),
                           oracle_fp="o", outcome=fs.RunOutcome.FLAT,
                           started="2026-01-01T00:00")]
    pieces = carve_rebuys(first, UnitService(rows, "o"), readmit_after=2)
    assert len(pieces) == 2
    assert {p.parent for p in pieces} == {run.key}


def _loc(s: str) -> FnLocator:
    f, n = s.split("::", 1)
    return FnLocator(f, n)


def _recorded(fill: dict[str, Any], fix: dict[str, Any]
              ) -> tuple[list[RouteRun], list[RouteLedgerRow], dict[str, Any]]:
    runs = []
    for r in fill["runs"]:
        kind = RouteKind(r["kind"])
        targets = sorted((_loc(t) for t in r["targets"]),
                         key=lambda u: (u.file, u.fn_name))
        tests = frozenset(r["target_tests"])
        runs.append(RouteRun(
            kind=kind, label=r["label"], targets=frozenset(targets),
            target_tests=tests,
            stubs_in_targets=frozenset(_loc(x) for x in r["stubs"]),
            files=tuple(r["files"]), seq=r["seq"], bodies_sha=r["bodies_sha"],
            unit_tests=(tuple((t, tests) for t in targets)
                        if kind is RouteKind.PER_FN else ())))
    rows = [r for r in (RouteLedgerRow.from_dict(x) for x in fix["route_ledger"]
                        if x.get("started", "")[:19] < fill["ts"])
            if r is not None]
    it = fill["iter"]
    entries = []
    for raw in fix["body_ledger"]:
        e = LedgerEntry.from_dict(raw)
        if e is None:
            continue
        landed = getattr(e.state, "value", e.state) == "landed"
        if e.run_id != fix["run_id"] or (
                e.iteration < it if landed else e.iteration <= it):
            entries.append(e)
    inp = fill["inputs"]
    kw = {"oracle_fp": fix["oracle_fp"],
          "hollow": {k: frozenset(_loc(x) for x in v)
                     for k, v in inp["hollow"].items()},
          "budget_minutes": fill["budget_minutes"],
          "readmit_after": inp["readmit_after"],
          "crash_blamed": frozenset(_loc(x) for x in inp["crash_blamed"]),
          "attributed": frozenset(_loc(x) for x in inp["attributed"]),
          "session_min_turns": inp["session_min_turns"],
          "max_per_kind": {RouteKind(k): v
                           for k, v in inp["max_per_kind"].items()},
          "due": frozenset(_loc(x) for x in inp["due"]),
          "unit_evidence": unit_evidence_from(entries, {},
                                              oracle_fp=fix["oracle_fp"])}
    return runs, rows, kw


_CONSTRUCTORS = {f"src/error.rs::construct_from_{s}"
                 for s in ("adhoc", "boxed", "context", "display", "std")}
#: The per-fn pieces the plan names, fill by fill.
_OPENED = {
    2: {"src/chain.rs::next", "src/context.rs::ext_context",
        "src/ensure.rs::render", "src/error.rs::construct_from_adhoc",
        "src/error.rs::construct_from_boxed",
        "src/error.rs::construct_from_display",
        "src/error.rs::construct_from_std", "src/error.rs::deref",
        "src/error.rs::from"},
    3: _CONSTRUCTORS | {"src/error.rs::object_boxed",
                        "src/error.rs::object_downcast",
                        "src/error.rs::object_drop", "src/error.rs::vtable"},
    4: _CONSTRUCTORS | {"src/error.rs::erase", "src/error.rs::error_mut",
                        "src/error.rs::from", "src/error.rs::msg",
                        "src/error.rs::new", "src/error.rs::object_drop_front"},
}
_WAITING = {2: {"src/lib.rs::must_use", "src/ptr.rs::cast", "src/ptr.rs::deref"},
            3: {"src/lib.rs::must_use", "src/ptr.rs::cast", "src/ptr.rs::deref"},
            4: {"src/lib.rs::format_err"}}


def _names(run: RouteRun) -> set[str]:
    return {f"{t.file}::{t.fn_name}" for t in run.targets}


@pytest.mark.parametrize("it", [2, 3, 4])
def test_recorded_fills_open_the_constructors_and_defer_only_other_file_extras(
        it: int) -> None:
    fix = json.loads(FIXTURE.read_text())
    fill = next(f for f in fix["fills"] if f["iter"] == it)
    runs, rows, kw = _recorded(fill, fix)

    sched = schedule(runs, rows, **kw)
    opened = [s.run for s in sched.ordered if s.run.kind is RouteKind.PER_FN]
    waiting = [s for s in sched.deferred if s.run.kind is RouteKind.PER_FN]
    assert [_names(r) for r in opened] == [_OPENED[it]]
    assert [(_names(s.run), s.reason) for s in waiting] == [
        (_WAITING[it], "coverage_split")]

    # Negative control: the pre-change schedule on the same inputs makes
    # the decision the live run printed — the whole per-fn run deferred
    # coverage_split — and every other run's live decision is reproduced.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "carve_coverage_split", lambda r, a: (r,))
        before = schedule(runs, rows, **kw)
    decided = {s.run.label: "opened" for s in before.ordered}
    decided |= {s.run.label: s.reason for s in before.deferred}
    assert decided == fill["live"]
    per_fn = [r["label"] for r in fill["runs"]
              if RouteKind(r["kind"]) is RouteKind.PER_FN]
    assert [fill["live"][label] for label in per_fn] == ["coverage_split"]


def test_the_piece_resolves_to_the_planned_route() -> None:
    """The executor looks a run up by `plan_key`; both pieces resolve to the
    planned per-fn run's key, so the route that planned it executes them."""
    fix = json.loads(FIXTURE.read_text())
    fill = next(f for f in fix["fills"] if f["iter"] == 3)
    runs, rows, kw = _recorded(fill, fix)
    planned = {r.key for r in runs}
    sched = schedule(runs, rows, **kw)
    for s in (*sched.ordered, *sched.deferred):
        assert s.run.plan_key in planned
    assert replace(runs[0]).plan_key == runs[0].key
