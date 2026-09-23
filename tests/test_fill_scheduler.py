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
from dataclasses import replace
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
    RouteLedgerRow,
    RouteRun,
    RunClock,
    RunOutcome,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.reasoning_cap import record_stream_meta, serving_units

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



def _moving_sha():
    """A `bodies_sha` whose every read differs: the tree moves under each
    run, so a reported keep is a landing. A constant stub would say the
    targets' bodies never changed, which the executor reads as a reply
    that re-emitted the tree (the kept set is dropped)."""
    n = iter(range(1_000_000))
    return lambda _t: f"b{next(n)}"

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
        out = execute_fill([_WaveRoute(labelled)], hollow={},
                           bodies_sha=_moving_sha(),
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


class _StopRoute(_Route):
    """A route whose run a graceful stop cuts: the executor's SystemExit path."""

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._calls.append(run.label)
        raise SystemExit(0)


def test_every_wave_hands_its_kept_units_to_the_unit_ledger_writer(
        tmp_path: Path) -> None:
    """The unit ledger's per-wave seam: `on_wave_kept` fires after each wave
    with the units the wave's reports kept, and once more on the wave a stop
    cuts, before the stop propagates. The sixty row-less bodies of
    2026-09-08 were kept by waves that completed inside fills that never
    returned; the control is the hook left unset, which is that silence."""
    from dataclasses import replace as _replace

    import pytest

    log: list[str] = []
    events: list[tuple[str, dict]] = []
    calls: list[str] = []
    seen: list[frozenset[FnLocator]] = []
    kept_run = RouteRun(kind=RouteKind.FILE, label="file src/x.rs",
                        targets=frozenset({_FMT}))
    cut_run = RouteRun(kind=RouteKind.PER_FN, label="per-fn 1 re-fill(s)",
                       targets=frozenset({_STUB}))
    routes = [
        _Route(RouteKind.FILE, [kept_run], calls=calls,
               report=RouteRunReport(model_calls=1, kept=frozenset({_FMT}))),
        _StopRoute(RouteKind.PER_FN, [cut_run], calls=calls),
    ]
    deps = _replace(_deps(tmp_path, log, events), on_wave_kept=seen.append)
    with pytest.raises(SystemExit):
        execute_fill(routes, hollow={}, bodies_sha=_moving_sha(), deps=deps)
    assert calls == ["file src/x.rs", "per-fn 1 re-fill(s)"]
    assert seen == [frozenset({_FMT}), frozenset()], (
        "one call per completed wave with its keeps, one for the cut wave")

    # THE CONTROL: no hook wired — the executor stops the same way and
    # tells nobody what the waves kept.
    calls_control: list[str] = []
    routes_control = [
        _Route(RouteKind.FILE, [kept_run], calls=calls_control,
               report=RouteRunReport(model_calls=1, kept=frozenset({_FMT}))),
        _StopRoute(RouteKind.PER_FN, [cut_run], calls=calls_control),
    ]
    # Its own ledger: the first fill's rows would rank the file run a
    # re-buy and put the stopping run first, which is a different fill.
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    with pytest.raises(SystemExit):
        execute_fill(routes_control, hollow={}, bodies_sha=lambda _t: "s",
                     deps=_deps(control_dir, [], []))
    assert calls_control == calls


class _StreamRoute(_Route):
    """A route that records one stream row per call, as the transport does.

    `finish_reason` is what the transport saw; `joined` says whether the
    call was bound to the units it serves, which is the difference between
    a fill call and a retrospective."""

    def __init__(self, *a, rows: Sequence[tuple[str, bool]], **kw) -> None:
        super().__init__(*a, **kw)
        self._rows = list(rows)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._calls.append(run.label)
        for finish, joined in self._rows:
            data = {"finish_reason": finish, "reasoning_chars": 120_000}
            if joined:
                with serving_units(f"{loc.file}::{loc.fn_name}"
                                   for loc in run.targets):
                    record_stream_meta(data)
            else:
                record_stream_meta(data)
        return RouteRunReport(model_calls=len(self._rows), kept=run.targets)


def _one_run_outcome(tmp_path: Path, rows: Sequence[tuple[str, bool]],
                     **kw) -> RunOutcome:
    fill_reading.reset_last_comparison()
    calls: list[str] = []
    route = _StreamRoute(RouteKind.PAIR, [RouteRun(
        kind=RouteKind.PAIR, label="pair src/x.rs::fmt + src/y.rs::new",
        targets=frozenset({_FMT}), target_tests=frozenset({"t_fmt"}))],
        calls=calls, rows=rows, **kw)
    out = execute_fill([route], hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, [], []))
    return out.rows[0].outcome


def test_a_run_whose_every_fill_reply_was_cut_is_graded_unmeasured(
        tmp_path: Path) -> None:
    """A truncated call is not a measured refusal, so the run that made it
    is UNMEASURED and the next schedule ranks it as never served — the rule
    the escalated batch route already applied per unit, at the route the
    fill phase now opens first.

    The negative control is the same run with the same keeps whose reply
    FINISHED: that one is a measured try and stays FLAT, which is what
    every capped run was graded before this."""
    assert _one_run_outcome(tmp_path, [("length", True)]) is RunOutcome.UNMEASURED
    assert _one_run_outcome(tmp_path / "b", [("stop", True)]) is RunOutcome.FLAT


def test_a_finished_diagnosis_call_does_not_make_a_cut_fill_call_measured(
        tmp_path: Path) -> None:
    """The pair's revert records a diagnosis through a second, unjoined
    call. It finishes; the fill call it describes did not. Only calls bound
    to the units they serve answer for the run."""
    assert _one_run_outcome(
        tmp_path, [("length", True), ("stop", False)]) is RunOutcome.UNMEASURED


def test_one_finished_fill_reply_among_several_keeps_the_run_measured(
        tmp_path: Path) -> None:
    """`unmeasured` is ALL of the run's fill replies, never any: a run that
    got one full turn was tried, whatever happened to its other calls."""
    assert _one_run_outcome(
        tmp_path, [("length", True), ("stop", True)]) is RunOutcome.FLAT


def test_the_executor_does_not_overrule_a_route_that_read_its_own_calls(
        tmp_path: Path) -> None:
    """The session grades itself: it can tell a capped reply that still
    carried a change set (judged) from one that carried none, which the
    stream rows alone cannot. A report that says it owns the reading keeps
    it."""
    fill_reading.reset_last_comparison()
    calls: list[str] = []

    class _OwnedRoute(_StreamRoute):
        def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
            rep = super().run(run, clock)
            return replace(rep, cap_owned=True)

    route = _OwnedRoute(RouteKind.SESSION, [_session()], calls=calls,
                        rows=[("length", True)])
    out = execute_fill([route], hollow={}, bodies_sha=lambda _t: "b",
                       deps=_deps(tmp_path, [], []))
    assert out.rows[0].outcome is RunOutcome.FLAT


def test_a_keep_that_left_the_targets_as_they_were_is_recorded_as_keeping_nothing(
        tmp_path: Path) -> None:
    """Run `3893442-cb547388` (2026-09-19): both hop pairs were rowed
    `kept 2` while the tree after the fill differed from the tree before
    it only in a file neither pair writes — the replies re-emitted the
    bodies on disk. The sentence: a run whose reported keeps leave its
    targets' bodies identical keeps nothing on its row, the unit-ledger
    hook hears no keep, the log says why, and the row is still a graded
    FLAT (the reply is evidence the re-buy detector needs).

    Control: the same run on a tree whose bodies moved keeps its unit."""
    from dataclasses import replace as _replace

    def one(sha, root: Path) -> tuple[object, list[str], list[frozenset[FnLocator]]]:
        log: list[str] = []
        seen: list[frozenset[FnLocator]] = []
        run = RouteRun(kind=RouteKind.FILE, label="file src/x.rs",
                       targets=frozenset({_FMT}))
        route = _Route(RouteKind.FILE, [run], calls=[],
                       report=RouteRunReport(model_calls=1, kept=frozenset({_FMT})))
        deps = _replace(_deps(root, log, []), on_wave_kept=seen.append)
        out = execute_fill([route], hollow={}, bodies_sha=sha, deps=deps)
        (row,) = out.rows
        return row, log, seen

    root = tmp_path / "same"
    root.mkdir()
    row, log, seen = one(lambda _t: "unchanged", root)
    assert row.kept == () and row.outcome is RunOutcome.FLAT
    assert row.model_calls == 1
    assert seen == [frozenset()]
    assert any("every kept body was the one already on disk" in ln for ln in log)

    root = tmp_path / "moved"
    root.mkdir()
    row, log, seen = one(_moving_sha(), root)
    assert row.kept == (("src/x.rs", "fmt"),)
    assert seen == [frozenset({_FMT})]
    assert not any("already on disk" in ln for ln in log)


def test_a_session_floor_still_binds_when_earlier_runs_burn_their_share(
        tmp_path: Path) -> None:
    """The plan reserves three turns before the split. The clock must open
    on that reservation: ten never-served runs ahead of a served session,
    each burning the share it was granted, still leave the session its
    floor. A weight split of only the time left gives the first run
    200 * 2/21 and the session whatever remains, under three turns."""
    t = [0.0]

    def now() -> float:
        return t[0]

    granted: list[tuple[str, float | None]] = []

    class _Burn(_Route):
        def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
            granted.append((run.label, clock.share_minutes))
            if clock.share_minutes:
                t[0] += clock.share_minutes * 60.0
            return RouteRunReport(model_calls=1)

    hub = FnLocator("src/s.rs", "hub")
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    deps = replace(_deps(tmp_path, log, events, budget=200, now=now),
                   session_min_turns=3)
    assert deps.ledger is not None
    deps.ledger.append("p", [RouteLedgerRow(
        kind=RouteKind.SESSION, label="earlier", bodies_sha="b",
        targets=(("src/s.rs", "hub"),), oracle_fp="o",
        outcome=RunOutcome.FLAT, cluster="c", minutes=36.0, model_calls=3,
        target_tests=("t_old",))])
    session = RouteRun(
        kind=RouteKind.SESSION, label="session", targets=frozenset({hub}),
        target_tests=frozenset({"t_new"}))
    per_fn = [
        RouteRun(kind=RouteKind.PER_FN, label=f"per-fn {i}",
                 targets=frozenset({FnLocator("src/p.rs", f"n{i}")}))
        for i in range(10)]
    out = execute_fill(
        [_Burn(RouteKind.PER_FN, per_fn, calls=[]),
         _Burn(RouteKind.SESSION, [session], calls=[])],
        hollow={}, bodies_sha=lambda _t: "b", deps=deps)
    plan = {s.run.label: s.share_minutes for s in out.schedule.ordered}
    assert granted[-1][0] == "session"
    assert plan["session"] is not None and plan["session"] >= 36.0 - 1e-6
    assert granted[-1][1] is not None and granted[-1][1] >= 36.0 - 1e-6
    # The unreserved split of the whole budget. The first clock is the
    # floor-reserved share, not this.
    assert granted[0][1] is not None and granted[0][1] < 200.0 * 2 / 21 - 1
    for label, clock_share in granted:
        assert clock_share is not None and plan[label] is not None
        assert abs(clock_share - plan[label]) < 1e-4


def test_a_carved_per_fn_run_is_executed_by_its_route_on_its_own_targets(
        tmp_path: Path) -> None:
    """The schedule carves a divisible run; the executor hands the open
    piece to the route that planned the whole run, with only the units that
    piece holds, and rows the re-buy piece DEFERRED under its own targets."""
    fill_reading.reset_last_comparison()
    a, b = FnLocator("src/p.rs", "a"), FnLocator("src/p.rs", "b")
    run = RouteRun(kind=RouteKind.PER_FN, label="per-fn 2 stub(s)",
                   targets=frozenset({a, b}),
                   target_tests=frozenset({"t_a", "t_b"}),
                   stubs_in_targets=frozenset({a, b}),
                   unit_tests=((a, frozenset({"t_a"})), (b, frozenset({"t_b"}))))
    seen: list[frozenset[FnLocator]] = []

    class _PerFn(_Route):
        def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
            seen.append(frozenset(run.targets))
            return super().run(run, clock)

    log: list[str] = []
    events: list[tuple[str, dict]] = []
    deps = _deps(tmp_path, log, events, budget=100.0)
    deps.ledger.append("p", [RouteLedgerRow(
        kind=RouteKind.PER_FN, label="per-fn", bodies_sha="old",
        targets=(("src/p.rs", "a"),), oracle_fp="o", outcome=RunOutcome.FLAT,
        model_calls=1, minutes=5.0, cluster="c",
        started="2026-01-01T00:00:00+00:00")])
    calls: list[str] = []
    out = execute_fill([_PerFn(RouteKind.PER_FN, [run], calls=calls)],
                       hollow={}, bodies_sha=_moving_sha(), deps=deps)
    assert seen == [frozenset({b})]
    deferred = [r for r in out.rows if r.outcome is RunOutcome.DEFERRED]
    assert [r.targets for r in deferred] == [(("src/p.rs", "a"),)]
