"""A blocker the fill's own keeps measure, that no run of the fill writes,
is written in the same fill.

Run `2355041-eae39441` (2026-10-27), fill 3: the per-fn keep of a
constructor put 19 tests' first failure in a callee no run of the fill
targeted (the file runs of that file aimed two other stubs), so both were
refused on premise and the fill ended with about 145 of 200 budget minutes
unspent; the callee waited a fill, and its own callee another. Fill 4
repeated it on a formatter's callee holding 47 tests. Plan sentence: when a
measured blocker has no writer ahead, a per-fn run is opened on it within
the fill, at the cursor, and the runs ahead on other branches still run.

NEGATIVE CONTROL: the executor without the opener (the code before the
change) writes the first layer and the unrelated file run, and never the
layer the keep measured.

Invented shape, no crate names.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.phase_c_engine import (
    _fold_per_fn_results,
)
from pipeline_core.domain.fill_class import FillClassHistogram
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import ClusterFillResult, FnLocator

_TOP = FnLocator("src/a.rs", "top")      # held at plan time
_MID = FnLocator("src/a.rs", "mid")      # what keeping `top` exposes
_LOW = FnLocator("src/a.rs", "low")      # what keeping `mid` exposes
_SIDE = FnLocator("src/c.rs", "side")    # another branch, its own file run
_STUBS = (_TOP, _MID, _LOW, _SIDE)


def _fn(fn: FnLocator) -> tuple[str, ...]:
    return ("fn", fn.file, fn.fn_name, "")


class _Tree:
    """A three-layer chain under t1 and t2, and a side branch under t3."""

    def __init__(self) -> None:
        self.kept: set[FnLocator] = set()

    def premise(self) -> dict:
        k = self.kept
        if _TOP not in k:
            chain = {"t1": _TOP, "t2": _TOP}
        elif _MID not in k:
            chain = {"t1": _MID, "t2": _MID}
        elif _LOW not in k:
            chain = {"t1": _LOW}
        else:
            chain = {}
        out: dict = dict.fromkeys(("t1", "t2"), (True, (), False))
        out.update({t: (False, _fn(u), False) for t, u in chain.items()})
        out["t3"] = ((True, (), False) if _SIDE in k
                     else (False, _fn(_SIDE), False))
        return out

    def held(self) -> dict[str, frozenset[FnLocator]]:
        held: dict[str, frozenset[FnLocator]] = {}
        for t, (passed, site, _a) in self.premise().items():
            if passed:
                continue
            loc = FnLocator(site[1], site[2])
            if loc in _STUBS and loc not in self.kept:
                held[t] = frozenset({loc})
        return held


class _PerUnit:
    """A per-fn route: claims and writes each target it is handed."""

    def __init__(self, planned: RouteRun | None, calls: list[str],
                 tree: _Tree, *, revert: frozenset[FnLocator] = frozenset()
                 ) -> None:
        self.kind = RouteKind.PER_FN
        self.parallel = False
        self._planned = planned
        self._calls = calls
        self._tree = tree
        self._revert = revert

    def plan(self) -> Sequence[RouteRun]:
        return [self._planned] if self._planned is not None else []

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        kept: set[FnLocator] = set()
        reverted: set[FnLocator] = set()
        for unit, tests in run.unit_tests:
            if clock.refusal(tests, [unit]):
                continue
            self._calls.append(unit.fn_name)
            if unit in self._revert:
                reverted.add(unit)
                continue
            self._tree.kept.add(unit)
            fill_reading.note_kept(0)
            kept.add(unit)
        return RouteRunReport(model_calls=len(kept) + len(reverted),
                              kept=frozenset(kept),
                              reverted=frozenset(reverted),
                              dispatched=frozenset(kept | reverted))


class _Whole:
    """A file route: one transaction over its targets."""

    def __init__(self, run: RouteRun, calls: list[str], tree: _Tree) -> None:
        self.kind = run.kind
        self.parallel = False
        self._run = run
        self._calls = calls
        self._tree = tree

    def plan(self) -> Sequence[RouteRun]:
        return [self._run]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        self._calls.append(run.label)
        self._tree.kept |= set(run.targets)
        fill_reading.note_kept(0)
        return RouteRunReport(model_calls=1, kept=run.targets)


def _side_run() -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label="file src/c.rs",
                    targets=frozenset({_SIDE}),
                    target_tests=frozenset({"t3"}),
                    stubs_in_targets=frozenset({_SIDE}), files=("src/c.rs",))


def _top_per_fn() -> RouteRun:
    return RouteRun(
        kind=RouteKind.PER_FN, label="per-fn 1 stub(s), 0 re-fill(s)",
        targets=frozenset({_TOP}), target_tests=frozenset({"t1", "t2"}),
        stubs_in_targets=frozenset({_TOP}), files=("src/a.rs",),
        unit_tests=((_TOP, frozenset({"t1", "t2"})),))


def _fill(tmp_path: Path, routes_of, *, hollow) -> tuple[list[str], list[str]]:
    fill_reading.reset_last_comparison()
    tree = _Tree()
    calls: list[str] = []
    events: list[str] = []
    fs.execute_fill(
        routes_of(calls, tree), hollow=hollow,
        bodies_sha=lambda _t: f"b{len(tree.kept)}",
        deps=fs.FillExecutionDeps(
            log=lambda _m: None,
            emit=lambda name, **_k: events.append(name),
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=3, oracle_fp="o",
            premise=tree.premise(), ground_now=tree.premise,
            held_now=tree.held))
    return calls, events


_HOLLOW = {"t1": frozenset({_TOP}), "t2": frozenset({_TOP}),
           "t3": frozenset({_SIDE})}


def _planned(calls: list[str], tree: _Tree) -> list:
    return [_PerUnit(_top_per_fn(), calls, tree),
            _Whole(_side_run(), calls, tree)]


def test_each_layer_a_keep_measures_is_written_in_the_same_fill(
        tmp_path: Path) -> None:
    calls, events = _fill(tmp_path, _planned, hollow=_HOLLOW)
    assert calls == ["top", "mid", "low", "file src/c.rs"]
    assert events.count("phase_c.fill.blocker_opened") == 2


def test_negative_control_without_the_opener_the_measured_layer_waits(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_open_for_blockers",
                        lambda ordered, *_a, **_k: ordered)
    calls, events = _fill(tmp_path, _planned, hollow=_HOLLOW)
    assert calls == ["top", "file src/c.rs"]
    assert "phase_c.fill.blocker_opened" not in events


def test_a_fill_whose_plan_holds_no_per_fn_run_still_opens_one(
        tmp_path: Path) -> None:
    # `side` is held at plan time with no run planned on it (the plan left
    # it out): only the layers the keeps measured are opened.
    top_file = RouteRun(kind=RouteKind.FILE, label="file src/a.rs",
                        targets=frozenset({_TOP}),
                        target_tests=frozenset({"t1", "t2"}),
                        stubs_in_targets=frozenset({_TOP}),
                        files=("src/a.rs",))

    def routes(calls: list[str], tree: _Tree) -> list:
        return [_Whole(top_file, calls, tree), _PerUnit(None, calls, tree)]

    calls, _events = _fill(tmp_path, routes, hollow=_HOLLOW)
    assert calls == ["file src/a.rs", "mid", "low"]


def test_a_blocker_the_opened_run_could_not_land_is_not_opened_again(
        tmp_path: Path) -> None:
    def routes(calls: list[str], tree: _Tree) -> list:
        return [_PerUnit(_top_per_fn(), calls, tree,
                         revert=frozenset({_MID})),
                _Whole(_side_run(), calls, tree)]

    calls, _events = _fill(tmp_path, routes, hollow=_HOLLOW)
    assert calls == ["top", "mid", "file src/c.rs"]


def test_the_fill_result_spans_every_per_fn_run() -> None:
    a = ClusterFillResult(fns_filled=2, attempts=3, errors=("e1",),
                          fill_classes=FillClassHistogram(unreachable=2))
    b = ClusterFillResult(fns_filled=1, attempts=1, remaining=7,
                          fill_classes=FillClassHistogram(cashed=1))
    got = _fold_per_fn_results(_fold_per_fn_results(None, a), b)
    assert (got.fns_filled, got.attempts, got.errors, got.remaining) == (
        3, 4, ("e1",), 7)
    assert got.fill_classes is not None
    assert got.fill_classes.as_dict()["unreachable"] == 2
    assert got.fill_classes.as_dict()["cashed"] == 1
