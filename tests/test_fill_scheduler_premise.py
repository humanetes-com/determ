"""A claim is refused once a keep moved the tests it was planned on.

A fill runs its schedule while each claim's premise holds; a claim (a run,
or a pooled unit) planned on a test a keep of this fill moved — its verdict,
or where it first fails — is refused, a run refused before it opens is
rowed DEFERRED/premise_moved, and the sweep re-plans it. Claims planned on
tests that did not move keep going. Run `3383938-c5d24499` (2026-09-19):
the first keep of fill 1 moved all 141 failing tests off `construct`'s
stub, and the rest of that fill ran a plan in which the new first failure
had no run. Run `497151-a6fa205b` (2026-09-22): the first keep moved 13
tests and the fill-wide gate refused 8 units none of whose tests moved.

Fake routes, the real executor and clocks. Negative control: the same fill
with no premise (`PHASE_C_REPLAN_ON_PREMISE=0`) runs both routes to the end.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    RunOutcome,
)
from pipeline_core.domain.models import FnLocator

_MAKE = FnLocator("src/h.rs", "make")
_RELEASE = FnLocator("src/h.rs", "release")
_OTHER = FnLocator("src/g.rs", "other")

_PLAN = {"t1": (False, ("fn", "src/h.rs", "make", ""), False),
         "t2": (False, ("fn", "src/h.rs", "make", ""), False)}
_MOVED = {"t1": (False, ("fn", "src/h.rs", "release", ""), False),
          "t2": (False, ("fn", "src/h.rs", "release", ""), False)}


class _Route:
    """Claims `units` one at a time through the clock; the first claimed
    unit keeps a body."""

    def __init__(self, kind: RouteKind, run: RouteRun, units: int,
                 calls: list[str], *, keeps: bool) -> None:
        self.kind = kind
        self.parallel = False
        self._run = run
        self._units = units
        self._calls = calls
        self._keeps = keeps

    def plan(self) -> Sequence[RouteRun]:
        return [self._run]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        claimed = 0
        for i in range(self._units):
            if not clock.may_claim():
                break
            claimed += 1
            self._calls.append(f"{run.label}#{i}")
            if self._keeps and i == 0:
                fill_reading.note_kept(0)
        return RouteRunReport(model_calls=claimed,
                              kept=run.targets if self._keeps else frozenset())


def _runs(b_tests: frozenset[str] = frozenset({"t3"})
          ) -> tuple[RouteRun, RouteRun]:
    a = RouteRun(kind=RouteKind.FILE, label="file src/h.rs",
                 targets=frozenset({_MAKE}), target_tests=frozenset({"t1", "t2"}),
                 stubs_in_targets=frozenset({_MAKE}), files=("src/h.rs",))
    b = RouteRun(kind=RouteKind.PER_FN, label="per-fn 1 stub(s)",
                 targets=frozenset({_OTHER}), target_tests=b_tests,
                 stubs_in_targets=frozenset({_OTHER}), files=("src/g.rs",))
    return a, b


def _deps(tmp_path: Path, log: list[str], events: list, *, premise, ground,
          reads: list[int]) -> FillExecutionDeps:
    def _ground():
        reads.append(1)
        return ground
    return FillExecutionDeps(
        log=log.append,
        emit=lambda ev, data=None, **_k: events.append((ev, data or {})),
        check_stop=lambda _c: None,
        ledger=RouteLedgerStore(tmp_path), project="p", cluster="c",
        run_id="r", iteration=1, oracle_fp="o",
        premise=premise, ground_now=_ground if premise is not None else None)


def _fill(tmp_path: Path, *, premise, ground,
          b_tests: frozenset[str] = frozenset({"t3"})):
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    events: list = []
    reads: list[int] = []
    a, b = _runs(b_tests)
    routes = [_Route(RouteKind.FILE, a, 3, calls, keeps=True),
              _Route(RouteKind.PER_FN, b, 2, calls, keeps=False)]
    out = execute_fill(routes, hollow={"t1": frozenset({_MAKE})},
                       bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, log, events, premise=premise,
                                  ground=ground, reads=reads))
    return out, calls, log, events, reads


def test_a_run_planned_on_a_moved_test_is_deferred(tmp_path: Path) -> None:
    out, calls, log, events, reads = _fill(
        tmp_path, premise=_PLAN, ground=_MOVED,
        b_tests=frozenset({"t2", "t3"}))
    # The unit in flight completed; its route's next claim was refused, and
    # the next run, planned on a moved test, never opened.
    assert calls == ["file src/h.rs#0"]
    assert out.premise_moved == ("t1", "t2")
    per_fn = [r for r in out.rows if r.label == "per-fn 1 stub(s)"]
    assert per_fn and per_fn[0].outcome is RunOutcome.DEFERRED
    assert per_fn[0].reason == "premise_moved"
    assert any("premise moved after file src/h.rs" in ln for ln in log)
    ev = [d for e, d in events if e == "phase_c.fill.premise_moved"][0]
    assert ev["moved_count"] == 2 and ev["continuing"] == 0
    # Measured once, after the keep — never before anything was kept.
    assert len(reads) == 1


def test_a_run_whose_planned_tests_did_not_move_runs_on(tmp_path: Path) -> None:
    """The keep moved t1 and t2; the per-fn run was planned on t3. Its
    premise holds, so it runs — the fill-wide gate deferred it."""
    out, calls, log, _events, reads = _fill(tmp_path, premise=_PLAN,
                                            ground=_MOVED)
    assert calls == ["file src/h.rs#0", "per-fn 1 stub(s)#0",
                     "per-fn 1 stub(s)#1"]
    assert out.premise_moved == ("t1", "t2")
    assert not any(r.reason == "premise_moved" for r in out.rows)
    assert not any("premise moved after file src/h.rs" in ln for ln in log)
    # One keep, one look: the ground is not re-read without a new keep.
    assert len(reads) == 1


def test_a_keep_that_moves_nothing_lets_the_fill_run_on(tmp_path: Path) -> None:
    out, calls, _log, _ev, reads = _fill(tmp_path, premise=_PLAN, ground=_PLAN)
    assert calls == ["file src/h.rs#0", "file src/h.rs#1", "file src/h.rs#2",
                     "per-fn 1 stub(s)#0", "per-fn 1 stub(s)#1"]
    assert out.premise_moved == ()
    assert len(reads) == 1  # one keep, one look


def test_negative_control_the_frozen_plan_runs_both_routes_out(
        tmp_path: Path) -> None:
    out, calls, _log, _ev, _reads = _fill(tmp_path, premise=None, ground=None)
    assert calls == ["file src/h.rs#0", "file src/h.rs#1", "file src/h.rs#2",
                     "per-fn 1 stub(s)#0", "per-fn 1 stub(s)#1"]
    assert out.premise_moved == ()


def test_a_single_run_fill_names_the_move_that_ended_it(tmp_path: Path) -> None:
    """A fill whose only run is the route that kept: no run is left to defer,
    so the between-waves line never prints. The gate itself names the move
    (run `311306-c8624d12`, 2026-09-22: a 12-unit per-fn batch stopped after
    four starts with nothing in the log saying why)."""
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    a, _b = _runs()
    out = execute_fill([_Route(RouteKind.FILE, a, 3, calls, keeps=True)],
                       hollow={"t1": frozenset({_MAKE})},
                       bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, log, [], premise=_PLAN,
                                  ground=_MOVED, reads=[]))
    assert calls == ["file src/h.rs#0"]
    assert out.premise_moved == ("t1", "t2")
    moved = [ln for ln in log if "premise moved after a keep" in ln]
    assert len(moved) == 1
    assert "Claims planned on a moved test are refused" in moved[0]
    # Negative control: the between-waves line cannot carry it here.
    assert not any("premise moved after file src/h.rs" in ln for ln in log)
