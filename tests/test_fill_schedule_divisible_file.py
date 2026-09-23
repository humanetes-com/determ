"""File and impl runs are ranked by unit, and only a revert that implicated a
unit convicts it.

Before 2026-09-23 a file or impl run was one transaction to the schedule: it
took its worst unit's class, so one unit whose own landing was reverted
deferred every other unit of the file. Run `271137-e728ae95` fill 1: `file
src/error.rs` and `impl src/error.rs` (7 targets each) were both deferred
`rebuy_reverted`. Ranked by unit on the same records, the file run opens
`from` and defers the six units the file route itself had reverted; the
impl run opens five and defers `construct_from_boxed` and `downcast_ref`.

Three sentences are pinned:

- a divisible file run opens the units its own record does not convict and
  defers the rest as a re-buy piece (`carve_rebuys`), on the recorded fill;
- a revert that restored a set whole (`collateral`) convicts no unit of it,
  while the identical transaction, same targets over the same bodies, is
  still the re-buy its identity row says;
- a carved file or impl run writes only what it holds: its blocks are
  narrowed and the carved units' bodies are withheld from the splice.

Each carries the transaction reading as its negative control.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport, RouteSnap
from pipeline_core.application.reconstruct.routes.file_first import FileRoute
from pipeline_core.application.reconstruct.routes.impl import ImplRoute
from pipeline_core.domain.fill_attempts import FillAttempts
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
    UnitEvidence,
    schedule,
    unit_evidence_from,
)
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_stamp import narrow_blocks_to

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "fills"
           / "271137-e728ae95_fill1_file_impl.json")
OR = "o1"
A, B, C = (FnLocator("src/x.rs", n) for n in ("a", "b", "c"))
# A never-served competitor: with every candidate a re-buy the schedule
# re-admits them all, which is not the question here.
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  target_tests=frozenset({"t_q"}),
                  bodies_sha="new")


def _loc(s: str) -> FnLocator:
    f, fn = s.split("::", 1)
    return FnLocator(f, fn)


def _pairs(units: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((u.file, u.fn_name) for u in units))


def _file_run(*, bodies: str = "new", divisible: bool = True) -> RouteRun:
    targets = frozenset({A, B, C})
    return RouteRun(
        kind=RouteKind.FILE, label="file src/x.rs", targets=targets,
        target_tests=frozenset({"t"}), stubs_in_targets=targets,
        files=("src/x.rs",), bodies_sha=bodies,
        unit_tests=(tuple((u, frozenset({"t"})) for u in (A, B, C))
                    if divisible else ()))


def _reverted_row(reverted: Any, *, collateral: Any = (),
                  bodies: str = "old") -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=RouteKind.FILE, label="file src/x.rs",
        targets=_pairs({A, B, C}), bodies_sha=bodies, oracle_fp=OR,
        outcome=RunOutcome.REVERTED, run_id="r1", iteration=1,
        started="2026-09-23T00:00:00", reverted=_pairs(reverted),
        collateral=_pairs(collateral))


def _opened_deferred(runs: list[RouteRun], rows: list[RouteLedgerRow]
                     ) -> tuple[set[FnLocator], set[FnLocator]]:
    s = schedule([*runs, _OTHER], rows, oracle_fp=OR, hollow={},
                 budget_minutes=100.0, unit_evidence=UnitEvidence())
    return ({t for x in s.ordered for t in x.run.targets} - _OTHER.targets,
            {t for x in s.deferred for t in x.run.targets})


# ---- a file run is ranked by unit ----


def test_an_implicated_revert_carves_its_unit_out_of_the_file() -> None:
    opened, deferred = _opened_deferred(
        [_file_run()], [_reverted_row({A}), ])
    assert opened == {B, C} and deferred == {A}


def test_control_the_file_as_a_transaction_defers_every_unit() -> None:
    opened, deferred = _opened_deferred(
        [_file_run(divisible=False)], [_reverted_row({A})])
    assert opened == set() and deferred == {A, B, C}


# ---- a collateral revert convicts no unit, the identical set still ----


def test_a_whole_set_revert_convicts_no_unit_of_a_moved_file() -> None:
    opened, deferred = _opened_deferred(
        [_file_run(bodies="new")],
        [_reverted_row({A, B, C}, collateral={A, B, C}, bodies="old")])
    assert opened == {A, B, C} and deferred == set()


def test_control_the_same_revert_without_the_collateral_mark_convicts_all() -> None:
    opened, deferred = _opened_deferred(
        [_file_run(bodies="new")],
        [_reverted_row({A, B, C}, bodies="old")])
    assert opened == set() and deferred == {A, B, C}


def test_the_identical_transaction_is_still_the_re_buy_its_row_says() -> None:
    """Same targets over the same bodies: the gate's revert of exactly this
    try stands, whatever it implicated."""
    opened, deferred = _opened_deferred(
        [_file_run(bodies="old")],
        [_reverted_row({A, B, C}, collateral={A, B, C}, bodies="old")])
    assert opened == set() and deferred == {A, B, C}


def test_the_attempt_ledger_types_a_set_revert_as_collateral() -> None:
    fa = FillAttempts(run_label=lambda: "")
    before = fa.snapshot()
    fa.note_dispatched([A, B, C])
    fa.note_reverted([A, B], collateral=True)
    fa.note_reverted([C], collateral=True)       # a set of one names it
    delta = FillAttempts.since(before, fa.snapshot())
    assert delta.reverted == {A, B, C} and delta.collateral == {A, B}
    fa.note_reverted([A])                       # a later implicated revert
    fa.note_kept([B])
    delta = FillAttempts.since(before, fa.snapshot())
    assert delta.collateral == frozenset()
    report = RouteRunReport.from_delta(delta, {})
    assert report.collateral == frozenset()


def test_a_whole_set_restore_keeps_a_peel_s_implication() -> None:
    """The file route notes the compiler's peel, then the gates reject the
    remainder and the pre-attempt file is restored as one transaction. The
    restore lists every landed body, including the peeled one. That body
    stays the unit the compiler named; the others are the transaction."""
    fa = FillAttempts(run_label=lambda: "")
    fa.note_dispatched([A, B, C])
    fa.note_reverted([A])
    fa.note_reverted([A, B, C], collateral=True)
    snap = fa.snapshot()
    assert snap.reverted == {A, B, C}
    assert snap.collateral == {B, C}


def test_a_mixed_row_defers_only_the_unit_the_revert_named() -> None:
    opened, deferred = _opened_deferred(
        [_file_run(bodies="new")],
        [_reverted_row({A, B, C}, collateral={B, C}, bodies="old")])
    assert opened == {B, C} and deferred == {A}


# ---- recorded: run 271137-e728ae95 fill 1 ----


def _recorded() -> tuple[dict[str, Any], list[RouteLedgerRow], UnitEvidence]:
    fix = json.loads(FIXTURE.read_text())
    rows = [r for r in (RouteLedgerRow.from_dict(x) for x in fix["ledger"])
            if r is not None]
    body = [SimpleNamespace(key=SimpleNamespace(file=b["file"], fn_name=b["fn"]),
                            run_id=b["run_id"], iteration=b["iteration"],
                            body_sha=b["body_sha"], state=b["state"],
                            oracle_fp=b["oracle_fp"])
            for b in fix["body_ledger"]]
    return fix, rows, unit_evidence_from(body, {}, oracle_fp=fix["oracle_fp"])


def _recorded_run(rec: dict[str, Any], *, divisible: bool) -> RouteRun:
    targets = sorted((_loc(x) for x in rec["targets"]),
                     key=lambda u: u.fn_name)
    tests = frozenset(rec["target_tests"])
    return RouteRun(
        kind=RouteKind(rec["kind"]), label=rec["label"],
        targets=frozenset(targets), target_tests=tests,
        stubs_in_targets=frozenset(_loc(x) for x in rec["stubs"]),
        files=tuple(rec["files"]), seq=rec["seq"],
        bodies_sha=rec["bodies_sha"],
        unit_tests=tuple((t, tests) for t in targets) if divisible else ())


def test_recorded_271137_fill_1_opens_the_unconvicted_units_of_error_rs() -> None:
    """Plan sentence, on the recorded fill: the file and impl runs of
    `src/error.rs` open the units their own records do not convict, and
    defer only the units whose own landing on that route was reverted. The
    transaction reading of the same inputs defers both whole, as live."""
    fix, rows, ev = _recorded()
    kw: dict[str, Any] = {"oracle_fp": fix["oracle_fp"], "hollow": {},
                          "budget_minutes": fix["budget_minutes"],
                          "readmit_after": 2, "unit_evidence": ev}
    runs = [_recorded_run(r, divisible=True) for r in fix["runs"]]
    s = schedule([*runs, _OTHER], rows, **kw)
    opened = {x.run.label: x.run.targets for x in s.ordered}
    deferred = {x.run.label: x.run.targets for x in s.deferred}
    for kind in ("file", "impl"):
        planned = next(r.targets for r in runs if r.label == f"{kind} src/error.rs")
        held = next(t for label, t in opened.items()
                    if label.startswith(f"{kind} src/error.rs ["))
        carved = next(t for label, t in deferred.items()
                      if label.startswith(f"{kind} src/error.rs ["))
        assert held and carved and held | carved == planned
        # Every deferred unit's own latest landing on this route reverted it.
        for u in carved:
            last = [r for r in rows if r.kind.value == kind
                    and (u.file, u.fn_name) in (*r.kept, *r.reverted)][-1]
            assert (u.file, u.fn_name) in last.reverted, u
    assert FnLocator("src/error.rs", "construct") in deferred[
        next(k for k in deferred if k.startswith("file src/error.rs ["))]
    # Control: the transaction reading defers both runs whole, as live.
    control = schedule([*(_recorded_run(r, divisible=False)
                          for r in fix["runs"]), _OTHER], rows, **kw)
    assert [x.run for x in control.ordered] == [_OTHER]
    assert sorted((x.run.label, x.reason) for x in control.deferred) == [
        ("file src/error.rs", "rebuy_reverted"),
        ("impl src/error.rs", "rebuy_reverted")]


# ---- a carved run writes only what it holds ----


def _rec(name: str) -> FnRecord:
    return FnRecord(file="src/x.rs", fn_name=name, container="impl X",
                    sig_text=f"fn {name}()", has_unimplemented_body=True)


_BLOCK = ImplBlock(file="src/x.rs", container="impl X",
                   members=(_rec("a"), _rec("b"), _rec("c")),
                   stub_members=(_rec("a"), _rec("b"), _rec("c")))


def test_narrowing_keeps_the_held_members_and_withholds_the_rest() -> None:
    blocks, withheld = narrow_blocks_to([_BLOCK], {B, C})
    assert [m.fn_name for m in blocks[0].stub_members] == ["b", "c"]
    assert withheld == {"a"}
    none, all_withheld = narrow_blocks_to([_BLOCK], set())
    assert none == [] and all_withheld == {"a", "b", "c"}


def _snap() -> RouteSnap:
    from pipeline_core.domain.fill_attempts import AttemptsSnapshot
    from pipeline_core.domain.fill_class import FillClassHistogram
    return RouteSnap(units=AttemptsSnapshot(), classes=FillClassHistogram())


def _clock() -> RunClock:
    return RunClock(share_minutes=None, fill_remaining=lambda: None)


def test_a_carved_file_run_writes_only_its_units() -> None:
    calls: list[tuple[list[str], frozenset[str]]] = []

    def fill_file(f: str, bs: list[ImplBlock], *, may_claim: Any,
                  withheld: frozenset[str]) -> None:
        calls.append(([m.fn_name for b in bs for m in b.stub_members], withheld))

    route = FileRoute(
        units_now=lambda: [("src/x.rs", [_BLOCK])], fill_file=fill_file,
        admits=lambda f, bs: True, snapshot=_snap,
        report_since=lambda s: RouteRunReport(), tests_for=lambda ls: frozenset(),
        stubs=frozenset(), on_plan=lambda u: None, log=lambda m: None)
    planned = route.plan()[0]
    assert {u for u, _ in planned.unit_tests} == {A, B, C}
    route.run(replace(planned, targets=frozenset({B}), parent=planned.key),
              _clock())
    route.run(planned, _clock())
    assert calls == [(["b"], frozenset({"a", "c"})),
                     (["a", "b", "c"], frozenset())]


def test_a_carved_impl_run_writes_only_its_units() -> None:
    calls: list[tuple[list[str], frozenset[str]]] = []

    def fill_block(b: ImplBlock, *, withheld: frozenset[str]) -> None:
        calls.append(([m.fn_name for m in b.stub_members], withheld))

    route = ImplRoute(
        blocks_now=lambda: [_BLOCK], fill_block=fill_block, snapshot=_snap,
        report_since=lambda s: RouteRunReport(), tests_for=lambda ls: frozenset(),
        stubs=frozenset(), log=lambda m: None)
    planned = route.plan()[0]
    assert {u for u, _ in planned.unit_tests} == {A, B, C}
    route.run(replace(planned, targets=frozenset({A, C}), parent=planned.key),
              _clock())
    assert calls == [(["a", "c"], frozenset({"b"}))]
