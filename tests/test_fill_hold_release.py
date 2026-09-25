"""A run the schedule held behind a run the executor then deferred is
released in the same fill.

The class, from run `116341-24c76a97` (2026-09-25, cluster `stable`), fill
6: the per-fn run opened first (it holds the stubs the failing tests stop
in), its keeps moved two tests, both formatter pairs were deferred
`premise_moved`, and the error.rs file run, its impl twin and a session —
held `pair_wait` behind those pairs by the schedule — were rowed deferred
for a wait on runs that never opened. The schedule already refuses such a
wait at planning time ("a run is held only by a run this pass has already
OPENED", run `3127-6763a5c6`); the executor's later deferral recreated it.

The plan's sentence: a held run whose every holder the executor deferred
opens in the same fill, after the premise gate has asked its own ground.
Negative control: with the release disabled the same fill rows the held run
`pair_wait` and never opens it. Invented shape, no crate names.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    RunOutcome,
    ScheduledRun,
    hollow_by_test,
    release_held,
    schedule,
)
from pipeline_core.domain.models import FnLocator, FnSite

_STUB = FnLocator("src/a.rs", "hold")
_FMT = FnLocator("src/a.rs", "show")
_NEW = FnLocator("src/b.rs", "make")
_PEEK = FnLocator("src/a.rs", "peek")


def _runs() -> tuple[RouteRun, RouteRun, RouteRun]:
    per_fn = RouteRun(kind=RouteKind.PER_FN, label="per-fn 1 stub(s)",
                      targets=frozenset({_STUB}),
                      target_tests=frozenset({"t_hollow"}),
                      stubs_in_targets=frozenset({_STUB}), files=("src/a.rs",),
                      bodies_sha="p")
    pair = RouteRun(kind=RouteKind.PAIR, label="pair src/a.rs::show + src/b.rs::make",
                    targets=frozenset({_FMT, _NEW}),
                    target_tests=frozenset({"t_show"}),
                    files=("src/a.rs", "src/b.rs"), bodies_sha="q")
    file_run = RouteRun(kind=RouteKind.FILE, label="file src/a.rs",
                        targets=frozenset({_FMT, _PEEK}),
                        target_tests=frozenset({"t_show", "t_peek"}),
                        files=("src/a.rs",), bodies_sha="f")
    return per_fn, pair, file_run


def _hollow() -> dict[str, frozenset[FnLocator]]:
    return hollow_by_test({"t_hollow": {FnSite("src/a.rs", "hold", "")}},
                          {_STUB})


def test_the_schedule_records_who_holds_a_run_and_its_rank_before_the_hold() -> None:
    per_fn, pair, file_run = _runs()
    sched = schedule([per_fn, pair, file_run], [], oracle_fp="o",
                     hollow=_hollow())
    held = [s for s in sched.deferred if s.run.label == file_run.label]
    assert held and held[0].reason == "pair_wait"
    assert held[0].held_by == (pair.key,)
    assert held[0].prior_reason and held[0].prior_reason != "pair_wait"
    assert [s.run.label for s in sched.ordered] == [per_fn.label, pair.label]


def test_release_needs_every_holder_gone() -> None:
    run = RouteRun(kind=RouteKind.FILE, label="f", targets=frozenset({_PEEK}),
                   files=("src/a.rs",))
    h = ScheduledRun(run=run, rank=0, weight=2, reason="pair_wait", level=2,
                     deferred=True, held_by=("k1", "k2"),
                     prior_reason="never_served")
    assert release_held([h], {"k1"}) == ()
    (out,) = release_held([h], {"k1", "k2"})
    assert not out.deferred and out.reason == "never_served"
    # A deferral with no recorded holder is not a hold.
    plain = ScheduledRun(run=run, rank=0, weight=0, reason="rebuy_flat",
                         level=2, deferred=True)
    assert release_held([plain], {"k1"}) == ()


class _Route:
    def __init__(self, run: RouteRun, calls: list[str], *, keeps: bool) -> None:
        self.kind = run.kind
        self.parallel = False
        self._run = run
        self._calls = calls
        self._keeps = keeps

    def plan(self) -> Sequence[RouteRun]:
        return [self._run]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        self._calls.append(run.label)
        if self._keeps:
            fill_reading.note_kept(0)
        return RouteRunReport(model_calls=1,
                              kept=run.targets if self._keeps else frozenset())


_PLAN = {"t_hollow": (False, ("fn", "src/a.rs", "hold", ""), False),
         "t_show": (False, ("fn", "src/a.rs", "hold", ""), False),
         "t_peek": (False, ("fn", "src/a.rs", "peek", ""), False)}
# The per-fn keep moves the tests that stopped in its stub; t_peek stays.
_NOW = {**_PLAN,
        "t_hollow": (True, (), False),
        "t_show": (False, ("fn", "src/c.rs", "later", ""), False)}


def _fill(tmp_path: Path) -> tuple[list[str], fs.FillExecution, list[str]]:
    fill_reading.reset_last_comparison()
    per_fn, pair, file_run = _runs()
    calls: list[str] = []
    log: list[str] = []
    out = fs.execute_fill(
        [_Route(per_fn, calls, keeps=True), _Route(pair, calls, keeps=False),
         _Route(file_run, calls, keeps=False)],
        hollow=_hollow(), bodies_sha=lambda _t: "b",
        deps=fs.FillExecutionDeps(
            log=log.append, emit=lambda *_a, **_k: None,
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=6, oracle_fp="o",
            premise=_PLAN, ground_now=lambda: _NOW))
    return calls, out, log


def test_a_run_held_behind_a_pair_the_executor_deferred_opens(
        tmp_path: Path) -> None:
    calls, out, log = _fill(tmp_path)
    by = {r.label: r for r in out.rows}
    assert by["pair src/a.rs::show + src/b.rs::make"].reason == "premise_moved"
    assert calls == ["per-fn 1 stub(s)", "file src/a.rs"]
    assert by["file src/a.rs"].outcome is not RunOutcome.DEFERRED
    assert any("file src/a.rs released" in ln for ln in log)


def test_negative_control_without_the_release_the_held_run_never_opens(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "release_held", lambda _h, _g: ())
    calls, out, _log = _fill(tmp_path)
    by = {r.label: r for r in out.rows}
    assert calls == ["per-fn 1 stub(s)"]
    assert by["file src/a.rs"].outcome is RunOutcome.DEFERRED
    assert by["file src/a.rs"].reason == "pair_wait"
