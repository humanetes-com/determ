"""The fill executor: routes are planned, ordered by the schedule, run under
clocks, recorded to the route ledger — with the plan's sentences as the
assertions and the fixed pre-schedule walk as the negative control.

- "the stub-holding run goes first; the session still opens in the same
  fill" (fake routes; today's order is the control);
- "a deferred run is recorded deferred and re-enters the next fill as a
  never-served candidate" (ledger round trip);
- "a run in flight is never interrupted; the clock only gates the next
  claim" (a route that polls the clock mid-run);
- "consecutive runs of a parallel route execute as one wave and are
  attributed their own keeps" (labelled keeps).
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
from pipeline_core.domain.fill_context import current_route
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    RunOutcome,
)
from pipeline_core.domain.models import FnLocator

_STUB = FnLocator("src/x.rs", "drop_rest")
_FMT = FnLocator("src/x.rs", "fmt")


class _Route:
    """A fake route: records claims and returns scripted reports."""

    def __init__(self, kind: RouteKind, runs: Sequence[RouteRun], *,
                 parallel: bool = False, calls: list[str],
                 report: RouteRunReport | None = None,
                 polls: int = 0, keep_delta: int | None = None) -> None:
        self.kind = kind
        self.parallel = parallel
        self._runs = list(runs)
        self._calls = calls
        self._report = report or RouteRunReport(model_calls=1)
        self._polls = polls
        self._keep_delta = keep_delta

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._calls.append(run.label)
        claimed = 0
        for _ in range(self._polls):
            if clock.may_claim():
                claimed += 1
        if self._keep_delta is not None:
            assert current_route() == (run.kind.value, run.label)
            fill_reading.note_kept(self._keep_delta)
        if self._polls:
            return RouteRunReport(model_calls=claimed, kept=run.targets)
        return self._report


def _deps(tmp_path: Path, log: list[str], events: list[tuple[str, dict]], *,
          budget: float = 0.0, scheduled: bool = True,
          now=None, parallelism: int = 1) -> FillExecutionDeps:
    kw = {}
    if now is not None:
        kw["now"] = now
    return FillExecutionDeps(
        log=log.append,
        emit=lambda ev, data=None, **_k: events.append((ev, data or {})),
        check_stop=lambda _c: None,
        ledger=RouteLedgerStore(tmp_path), project="p", cluster="c",
        run_id="r", iteration=1, oracle_fp="o", budget_minutes=budget,
        scheduled=scheduled, parallelism=parallelism, **kw)


def _session() -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label="session[1 test(s)]",
                    targets=frozenset({_FMT}), target_tests=frozenset({"t_fmt"}))


def _file() -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label="file src/x.rs",
                    targets=frozenset({_STUB, _FMT}),
                    target_tests=frozenset({"t_hollow", "t_fmt"}),
                    stubs_in_targets=frozenset({_STUB}), files=("src/x.rs",))


def test_the_stub_run_goes_first_and_the_session_still_opens(tmp_path: Path) -> None:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    routes = [_Route(RouteKind.SESSION, [_session()], calls=calls),
              _Route(RouteKind.FILE, [_file()], parallel=True, calls=calls)]
    hollow = {"t_hollow": frozenset({_STUB})}
    out = execute_fill(routes, hollow=hollow, bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, log, events))
    assert calls == ["file src/x.rs", "session[1 test(s)]"]
    assert [r.label for r in out.rows] == calls
    assert any(ln.startswith("  [phase-c/schedule] fill 1: 2 run(s) — 1. file src/x.rs")
               for ln in log)
    kinds = [ev for ev, _ in events]
    assert kinds[0] == "phase_c.fill.schedule" and kinds.count("phase_c.route.run") == 2
    # Negative control: the fixed pre-schedule walk opens the session first.
    calls.clear()
    execute_fill(routes, hollow=hollow, bodies_sha=lambda _t: "b",
                 deps=_deps(tmp_path, log, events, scheduled=False))
    assert calls == ["session[1 test(s)]", "file src/x.rs"]


def test_rows_are_written_and_a_flat_rebuy_is_deferred_next_fill(tmp_path: Path) -> None:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    flat = RouteRunReport(model_calls=2, kept=frozenset({_STUB}))
    routes = [_Route(RouteKind.FILE, [_file()], calls=calls, report=flat),
              _Route(RouteKind.PER_FN, [RouteRun(
                  kind=RouteKind.PER_FN, label="per-fn", targets=frozenset({_STUB}),
                  stubs_in_targets=frozenset({_STUB}))], calls=calls)]
    hollow = {"t_hollow": frozenset({_STUB})}
    first = execute_fill(routes, hollow=hollow, bodies_sha=lambda _t: "same",
                         deps=_deps(tmp_path, log, events, budget=60))
    assert [r.outcome for r in first.rows] == [RunOutcome.FLAT, RunOutcome.FLAT]
    stored = RouteLedgerStore(tmp_path).load("p", "c")
    assert len(stored) == 2 and stored[0].label == "file src/x.rs"
    # Same bodies next fill: both are re-buys; every candidate a re-buy →
    # shared budget (nothing deferred). With a never-served neighbour, the
    # re-buys defer.
    calls.clear()
    fresh = _Route(RouteKind.SESSION, [_session()], calls=calls)
    second = execute_fill([*routes, fresh], hollow=hollow,
                          bodies_sha=lambda _t: "same",
                          deps=_deps(tmp_path, log, events, budget=60))
    assert calls == ["session[1 test(s)]"]
    deferred = [r for r in second.rows if r.outcome is RunOutcome.DEFERRED]
    assert {r.label for r in deferred} == {"file src/x.rs", "per-fn"}
    assert all(r.reason in ("rebuy_flat",) for r in deferred)


def test_the_clock_gates_the_next_claim_never_the_unit_in_flight(tmp_path: Path) -> None:
    fill_reading.reset_last_comparison()
    t = [0.0]

    def now() -> float:
        t[0] += 5 * 60  # every clock read is five minutes later
        return t[0]

    calls: list[str] = []
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    poller = _Route(RouteKind.FILE, [_file()], calls=calls, polls=20)
    out = execute_fill([poller], hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, log, events, budget=60, now=now))
    row = out.rows[0]
    # Claims are granted while the share lasts and refused once it is spent
    # with nothing paid — the run itself completed and was recorded.
    assert 1 <= row.model_calls < 20
    assert row.outcome is RunOutcome.FLAT
    assert row.share_minutes is not None and 0 < row.share_minutes <= 60.0


def test_a_wave_of_parallel_runs_attributes_its_own_keeps(tmp_path: Path) -> None:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    a = RouteRun(kind=RouteKind.FILE, label="file a.rs", targets=frozenset({FnLocator("a.rs", "f")}), files=("a.rs",))
    b = RouteRun(kind=RouteKind.FILE, label="file b.rs", targets=frozenset({FnLocator("b.rs", "g")}), files=("b.rs",), seq=1)
    route = _Route(RouteKind.FILE, [a, b], parallel=True, calls=calls, keep_delta=3)
    out = execute_fill([route], hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, log, events, parallelism=2))
    assert sorted(calls) == ["file a.rs", "file b.rs"]
    by_label = {r.label: r for r in out.rows}
    assert by_label["file a.rs"].net_pass_delta == 3
    assert by_label["file b.rs"].net_pass_delta == 3
    assert all(r.outcome is RunOutcome.CASHED for r in out.rows)


def test_the_executor_opens_each_equal_count_session_once_in_schedule_order(
        tmp_path: Path) -> None:
    """Plan sentence: execute_fill on a session route whose plan() returns
    two equal-count slices calls run_one once per slice with that slice's
    tests, in the schedule's order rather than plan() list order.

    Fill 1 opens the first slice alone and it goes FLAT; in fill 2 that
    slice is a re-buy and the second is never-served, so the schedule puts
    the second first — the reverse of plan() order — and the schedule line
    tells the two count-labelled runs apart. Negative control, through the
    same executor: the pre-fix route (the bare count label as the lookup
    key, last write wins) opens the second slice twice in fill 2 and the
    first never."""
    from dataclasses import replace

    from pipeline_core.application.reconstruct.routes import SessionRoute
    from pipeline_core.domain.models import FnSite
    from pipeline_core.domain.slice_session import SessionSlice

    a = SessionSlice(
        tests=("t_a", "t_b"),
        units=(FnSite("src/lib.rs", "fmt", "impl Display"),
               FnSite("src/render.rs", "render", "impl Renderer")))
    b = SessionSlice(
        tests=("t_c", "t_d"),
        units=(FnSite("src/lib.rs", "fmt", "impl Display"),
               FnSite("src/other.rs", "go", "free fn")))
    assert a.label() == b.label()

    class _PreFixSessionRoute(SessionRoute):
        """The session route before binding moved to the aimed tests."""

        def plan(self) -> Sequence[RouteRun]:
            return [replace(run, label=sl.label())
                    for run, sl in zip(super().plan(), self._plans, strict=True)]

        def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
            sl = {p.label(): p for p in self._plans}.get(run.label)
            if sl is None:
                return RouteRunReport(empty=True)
            return self._run_one(sl, clock)

    def two_fills(route_cls: type[SessionRoute], root: Path) -> tuple[
            list[tuple[str, ...]], list[str], list[tuple[str, dict]]]:
        root.mkdir()
        opened: list[tuple[str, ...]] = []

        def run_one(sl: SessionSlice, clock: RunClock) -> RouteRunReport:
            opened.append(sl.tests)
            clock.may_claim()
            return RouteRunReport(model_calls=1)

        log: list[str] = []
        events: list[tuple[str, dict]] = []
        for plans in ([a], [a, b]):
            fill_reading.reset_last_comparison()
            route = route_cls(plans, run_one, served=set(), log=lambda _s: None)
            execute_fill([route], hollow={}, bodies_sha=lambda _t: "b",
                         deps=_deps(root, log, events))
        return opened, log, events

    opened, log, events = two_fills(SessionRoute, tmp_path / "shipped")
    assert opened[0] == a.tests                       # fill 1: the seed
    assert opened[1:] == [b.tests, a.tests]           # fill 2: schedule order
    line = [ln for ln in log if ln.startswith("  [phase-c/schedule] fill 1: 2 run(s)")][-1]
    assert line.index("#1 (never_served)") < line.index("#0 (rebuy_flat)")
    rows = [d for ev, d in events if ev == "phase_c.route.run"][-2:]
    assert [tuple(r["target_tests"]) for r in rows] == [b.tests, a.tests]

    opened, _log, _events = two_fills(_PreFixSessionRoute, tmp_path / "prefix")
    assert opened[0] == a.tests
    assert opened[1:] == [b.tests, b.tests]


def test_a_wave_s_rows_carry_each_run_s_own_units_and_a_run_that_never_opened_is_empty(
        tmp_path: Path) -> None:
    """Plan sentence: a wave of file runs on disjoint files, run
    concurrently through `execute_fill` over one attempt ledger, where run
    A keeps its unit, run B reverts its unit and run C returns without a
    dispatch or a call: A's row is `kept == {A's unit}`, `model_calls ==
    A's calls`, FLAT; B's row is `reverted == {B's unit}`, `kept == ∅`,
    REVERTED; C's row is EMPTY. Negative control, the same wave read off
    the whole tick window: B and C carry A's keep and grade FLAT, A
    carries B's revert, and every row's calls are the wave's."""
    import threading

    from pipeline_core.domain.fill_attempts import FillAttempts, current_run_label

    a = RouteRun(kind=RouteKind.FILE, label="file a.rs",
                 targets=frozenset({FnLocator("a.rs", "f")}), files=("a.rs",))
    b = RouteRun(kind=RouteKind.FILE, label="file b.rs",
                 targets=frozenset({FnLocator("b.rs", "g")}), files=("b.rs",), seq=1)
    c = RouteRun(kind=RouteKind.FILE, label="file c.rs",
                 targets=frozenset({FnLocator("c.rs", "h")}), files=("c.rs",), seq=2)

    class _WaveRoute:
        """Three runs in flight at once: every window holds every note."""

        kind = RouteKind.FILE
        parallel = True

        def __init__(self, labelled: bool) -> None:
            self._ledger = FillAttempts()
            self._gate = threading.Barrier(3, timeout=30)
            self._labelled = labelled

        def plan(self) -> Sequence[RouteRun]:
            return [a, b, c]

        def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
            before = self._ledger.snapshot()
            self._gate.wait()                       # every run has opened
            if run.label == "file a.rs":
                self._ledger.note_dispatched(run.targets)
                self._ledger.note_call()
                self._ledger.note_kept(run.targets)
            elif run.label == "file b.rs":
                self._ledger.note_dispatched(run.targets)
                self._ledger.note_call(2)
                self._ledger.note_reverted(run.targets)
            self._gate.wait()                       # every run has landed
            delta = FillAttempts.since(
                before, self._ledger.snapshot(),
                current_run_label() if self._labelled else None)
            return RouteRunReport.from_delta(delta, {})

    def rows(labelled: bool, root: Path) -> dict[str, object]:
        fill_reading.reset_last_comparison()
        log: list[str] = []
        events: list[tuple[str, dict]] = []
        out = execute_fill([_WaveRoute(labelled)], hollow={}, bodies_sha=lambda _t: "b",
                           deps=_deps(root, log, events, parallelism=3))
        return {r.label: r for r in out.rows}

    by = rows(True, tmp_path / "labelled")
    assert by["file a.rs"].outcome is RunOutcome.FLAT
    assert by["file a.rs"].kept == (("a.rs", "f"),) and by["file a.rs"].reverted == ()
    assert by["file a.rs"].model_calls == 1
    assert by["file b.rs"].outcome is RunOutcome.REVERTED
    assert by["file b.rs"].reverted == (("b.rs", "g"),) and by["file b.rs"].kept == ()
    assert by["file b.rs"].model_calls == 2
    assert by["file c.rs"].outcome is RunOutcome.EMPTY
    assert by["file c.rs"].kept == () and by["file c.rs"].model_calls == 0

    old = rows(False, tmp_path / "window")
    assert old["file b.rs"].outcome is RunOutcome.FLAT
    assert ("a.rs", "f") in old["file b.rs"].kept
    assert old["file c.rs"].outcome is RunOutcome.FLAT
    assert old["file c.rs"].kept == (("a.rs", "f"),)
    assert ("b.rs", "g") in old["file a.rs"].reverted
    assert {r.model_calls for r in old.values()} == {3}
