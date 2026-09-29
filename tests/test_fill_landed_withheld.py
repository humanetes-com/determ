"""A body an earlier run of the fill kept is not rewritten by a later run of
the same fill: the sweep is the first reading that can grade it.

Run `143676-7dadc500` (2026-09-24, cluster `stable`), iteration 3: the file
runs kept bodies across `src/chain.rs`, `src/error.rs` and `src/ptr.rs`, and
then the per-fn run re-filled 10 of its 12 targets from that same set — 86
min, 23 calls, no verdict moved, and the file runs' bodies were overwritten
before any sweep could grade them. The per-impl route already waited for the
sweep after a file-first or pair landing; the per-fn route asked neither.
`fill_schedule.withhold_landed` answers the question once, in the executor,
for every run it opens.

Each executor test carries its negative control: the same fill under
`scheduled=False` (the pre-schedule walk, `PHASE_C_FILL_SCHEDULE=0`) hands
the later run its whole planned target set.
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
    withhold_landed,
)
from pipeline_core.domain.models import FnLocator

_A = FnLocator("src/p.rs", "a")
_B = FnLocator("src/p.rs", "b")
_C = FnLocator("src/q.rs", "c")


class _Route:
    """A fake route: keeps every target it is handed, and records them."""

    def __init__(self, kind: RouteKind, runs: Sequence[RouteRun],
                 seen: list[tuple[str, frozenset[FnLocator]]]) -> None:
        self.kind = kind
        self.parallel = False
        self._runs = list(runs)
        self._seen = seen

    def plan(self) -> Sequence[RouteRun]:
        return list(self._runs)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        self._seen.append((run.kind.value, frozenset(run.targets)))
        clock.may_claim()
        return RouteRunReport(model_calls=1, kept=frozenset(run.targets))


def _moving_sha():
    n = iter(range(1_000_000))
    return lambda _t: f"b{next(n)}"


def _deps(tmp_path: Path, log: list[str], events: list[tuple[str, dict]], *,
          scheduled: bool = True) -> FillExecutionDeps:
    return FillExecutionDeps(
        log=log.append,
        emit=lambda ev, data=None, **_k: events.append((ev, data or {})),
        check_stop=lambda _c: None,
        ledger=RouteLedgerStore(tmp_path), project="p", cluster="c",
        run_id="r", iteration=3, oracle_fp="o", scheduled=scheduled)


def _file(*targets: FnLocator) -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label="file src/p.rs",
                    targets=frozenset(targets), target_tests=frozenset({"t"}),
                    files=("src/p.rs",),
                    unit_tests=tuple((t, frozenset({"t"})) for t in targets))


def _per_fn(*targets: FnLocator) -> RouteRun:
    return RouteRun(kind=RouteKind.PER_FN,
                    label=f"per-fn 0 stub(s), {len(targets)} re-fill(s)",
                    targets=frozenset(targets), target_tests=frozenset({"t"}),
                    files=tuple(sorted({t.file for t in targets})),
                    unit_tests=tuple((t, frozenset({"t"})) for t in targets))


def _fill(tmp_path: Path, later: RouteRun, *, scheduled: bool = True):
    fill_reading.reset_last_comparison()
    seen: list[tuple[str, frozenset[FnLocator]]] = []
    log: list[str] = []
    events: list[tuple[str, dict]] = []
    routes = [_Route(RouteKind.FILE, [_file(_A, _B)], seen),
              _Route(later.kind, [later], seen)]
    out = execute_fill(routes, hollow={}, bodies_sha=_moving_sha(),
                       deps=_deps(tmp_path, log, events, scheduled=scheduled))
    return seen, log, events, out


def test_a_later_per_fn_run_does_not_rewrite_what_the_file_run_kept(
        tmp_path: Path) -> None:
    """The plan's sentence: the per-fn run opens on the units no earlier run
    of the fill kept, and the withheld ones are named on a log line and an
    event."""
    seen, log, events, _out = _fill(tmp_path, _per_fn(_A, _C))
    assert seen == [("file", frozenset({_A, _B})), ("per-fn", frozenset({_C}))]
    line = [ln for ln in log if "withheld until the sweep grades them" in ln]
    assert line and "src/p.rs::a" in line[0], log
    ev = [d for e, d in events if e == "phase_c.fill.landed_withheld"]
    assert ev == [{"cluster": "c", "iter": 3,
                   "run": "per-fn 0 stub(s), 2 re-fill(s)",
                   "withheld": ["src/p.rs::a"], "deferred": False,
                   "open": ["src/q.rs::c"]}]

    # NEGATIVE CONTROL: the pre-schedule walk hands the per-fn run its whole
    # planned set, the file run's kept body included.
    control = tmp_path / "control"
    control.mkdir()
    c_seen, c_log, _e, _o = _fill(control, _per_fn(_A, _C), scheduled=False)
    assert c_seen[-1] == ("per-fn", frozenset({_A, _C}))
    assert not any("withheld until the sweep" in ln for ln in c_log)


def test_a_run_whose_every_target_landed_is_deferred_and_rowed(
        tmp_path: Path) -> None:
    """Nothing left to open: the run is not opened at all and its row says
    why, so the next fill's schedule sees a deferral, not an empty try."""
    seen, _log, _events, out = _fill(tmp_path, _per_fn(_A, _B))
    assert seen == [("file", frozenset({_A, _B}))]
    deferred = [r for r in out.rows if r.outcome is RunOutcome.DEFERRED]
    assert [r.reason for r in deferred] == ["landed_this_fill"]


def test_a_pair_is_a_transaction_and_defers_whole(tmp_path: Path) -> None:
    """A pair writes its two endpoints as one change set; carving one of
    them off would open a different question. One endpoint landed defers
    the pair."""
    pair = RouteRun(kind=RouteKind.PAIR, label="pair a + c",
                    targets=frozenset({_A, _C}), target_tests=frozenset({"t"}),
                    files=("src/p.rs", "src/q.rs"))
    run, held = withhold_landed(pair, {_A})
    assert run is None and held == {_A}


def test_a_session_is_never_withheld() -> None:
    """A session's try is keyed by the tests it aims at, not by the units its
    transcript touches, and its turns are judged as they land."""
    session = RouteRun(kind=RouteKind.SESSION, label="session[1 test(s)]",
                       targets=frozenset({_A, _C}),
                       target_tests=frozenset({"t"}))
    assert withhold_landed(session, {_A}) == (session, frozenset())


def test_a_carved_piece_of_a_carved_run_still_names_the_planned_run() -> None:
    """The executor finds the route by `plan_key`; a second carve must keep
    pointing at the run the route planned, not at the first piece."""
    planned = _per_fn(_A, _B, _C)
    first, _ = withhold_landed(planned, {_A})
    assert first is not None and first.parent == planned.key
    second, _ = withhold_landed(first, {_B})
    assert second is not None and second.plan_key == planned.key
    assert second.targets == {_C}


def test_the_line_names_every_open_target_after_opens_on_the_other(
        tmp_path: Path) -> None:
    """The plan's sentence: the names after `opens on the other N:` are the
    complete open set — every one, including a name that sorts past the
    fourth — and no withheld name; the withheld name appears only in the
    withheld clause. Run `473471-33301154` (2026-09-29, fill 6) printed the
    held names after `opens on the other 6`."""
    opens = [FnLocator("src/q.rs", n) for n in ("c", "d", "e", "f", "g")]
    last = FnLocator("src/z.rs", "zz")
    _seen, log, events, _out = _fill(tmp_path, _per_fn(_A, *opens, last))
    line = next(ln for ln in log if "withheld until the sweep grades" in ln)
    head, sep, open_clause = line.partition("opens on the other 6: ")
    assert sep, line
    want = sorted(f"{u.file}::{u.fn_name}" for u in (*opens, last))
    assert open_clause.split(", ") == want
    assert "src/p.rs::a" not in open_clause
    assert "withheld: src/p.rs::a;" in head
    ev = next(d for e, d in events if e == "phase_c.fill.landed_withheld")
    assert ev["open"] == want

    # NEGATIVE CONTROL: the previous writer's tail, `f"{what}: {names}"`
    # with `what = "opens on the other N"`, puts the held names in the open
    # clause and none of the open ones.
    old_tail = f"opens on the other {len(want)}: src/p.rs::a"
    _h, _s, old_open = old_tail.partition(f"opens on the other {len(want)}: ")
    assert old_open.split(", ") != want and "src/p.rs::a" in old_open


def test_a_fully_withheld_run_prints_deferred_and_no_open_clause(
        tmp_path: Path) -> None:
    _seen, log, _events, _out = _fill(tmp_path, _per_fn(_A, _B))
    line = next(ln for ln in log if "withheld until the sweep grades" in ln)
    assert "deferred: src/p.rs::a, src/p.rs::b" in line
    assert "opens on the other" not in line
