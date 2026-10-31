"""A holder a keep measures, that no run writes, is written at the next claim,
not after the run in flight.

Run `15370-6a4cfd45` (2026-10-30), fill 1: the keep of a caller at 18:05:44
UTC named a still-stubbed callee as the function its tests now stop in. The
16-unit per-fn run in flight went on claiming the callee's other callers,
ten more keeps named the same callee, and the run that wrote it opened at
19:06:37, when the wave ended and the boundary opener read the held view.
Plan sentence: one gate answers every claim; for a per-fn run in flight the
answer admits the holder into the run's own claim queue ahead of its
unclaimed units (the holder is claimed before the next caller), and for a
run of another route it opens the per-fn run at the cursor. No reaction to
the ground lives outside the gate.

NEGATIVE CONTROL: a run that cannot take a holder (the pre-change per-fn
run, whose pool was fixed at open) claims every caller first and the holder
is written after the run; a holder that is a later target of the same run
is not pulled forward either.

Invented shape, no crate names.
"""
from __future__ import annotations

import ast
import inspect
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.fill_schedule import (
    HolderAdmission,
    HolderQueue,
    RouteKind,
    RouteRun,
    RunClock,
)
from pipeline_core.domain.models import FillResult, FnLocator, FnSite

_CALLERS = tuple(FnLocator("src/a.rs", f"c{n}") for n in (1, 2, 3))
_HOLDER = FnLocator("src/p.rs", "h")
_HOLDER_SITE = FnSite(_HOLDER.file, _HOLDER.fn_name, "")


def _pname(c: FnLocator, doc: bool) -> str:
    """A caller's test as the premise keys it (a doc test by its `#k`)."""
    return f"src/a.rs - {c.fn_name}#0" if doc else f"t_{c.fn_name}"


def _hname(c: FnLocator, doc: bool) -> str:
    """The same test as the held view and the sweep key it (a doc test by
    libtest's `(line N)`)."""
    return (f"src/a.rs - {c.fn_name} (line {10 + _CALLERS.index(c)})"
            if doc else f"t_{c.fn_name}")


class _Tree:
    """Each caller's test stops in the caller; once the caller is written it
    stops in the shared holder, until the holder is written."""

    def __init__(self, doc: bool = False) -> None:
        self.kept: set[FnLocator] = set()
        self.doc = doc

    def _stop(self, caller: FnLocator) -> FnLocator | None:
        if caller not in self.kept:
            return caller
        return None if _HOLDER in self.kept else _HOLDER

    def premise(self) -> dict:
        out: dict = {}
        for c in _CALLERS:
            stop = self._stop(c)
            out[_pname(c, self.doc)] = ((True, (), False) if stop is None
                                     else (False, ("fn", stop.file, stop.fn_name, ""), False))
        return out

    def held(self) -> dict[str, frozenset[FnSite]]:
        return {_hname(c, self.doc): frozenset({FnSite(s.file, s.fn_name, "")})
                for c in _CALLERS if (s := self._stop(c)) is not None}


class _PerUnit:
    """A per-fn route that claims each unit on its own and, when it can take
    a holder (`takes_holders`), writes what the gate hands it first — the
    loop `_per_fn_passes` runs, without the engine."""

    def __init__(self, planned: RouteRun | None, calls: list[str],
                 tree: _Tree, *, takes: bool = True) -> None:
        self.kind = RouteKind.PER_FN
        self.parallel = False
        self.takes_holders = takes
        self._planned = planned
        self._calls = calls
        self._tree = tree

    def plan(self) -> Sequence[RouteRun]:
        return [self._planned] if self._planned is not None else []

    def _write(self, unit: FnLocator, tests: frozenset[str], clock: RunClock,
               kept: set[FnLocator]) -> None:
        if unit in kept:
            return
        while True:
            why = clock.refusal(tests, [unit])
            if why != "take":
                break
            for adm in clock.take_holders():
                for site, ts in adm.held_tests:
                    self._write(FnLocator(site.file, site.fn_name), ts,
                                clock, kept)
        if why:
            return
        self._calls.append(unit.fn_name)
        self._tree.kept.add(unit)
        fill_reading.note_kept(0)
        kept.add(unit)

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        kept: set[FnLocator] = set()
        for unit, tests in run.unit_tests:
            self._write(unit, tests, clock, kept)
        return RouteRunReport(model_calls=len(kept), kept=frozenset(kept),
                              dispatched=frozenset(kept))


class _Whole:
    """A file route: one transaction over its targets."""

    def __init__(self, runs: Sequence[RouteRun], calls: list[str],
                 tree: _Tree) -> None:
        self.kind = RouteKind.FILE
        self.parallel = True
        self._runs = list(runs)
        self._calls = calls
        self._tree = tree

    def plan(self) -> Sequence[RouteRun]:
        return self._runs

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        self._calls.append(run.label)
        self._tree.kept |= set(run.targets)
        fill_reading.note_kept(0)
        return RouteRunReport(model_calls=1, kept=run.targets)


def _per_fn_run(units: Sequence[FnLocator], doc: bool = False) -> RouteRun:
    tests = tuple((u, frozenset({_hname(u, doc)}) if u in _CALLERS
                   else frozenset()) for u in units)
    return RouteRun(
        kind=RouteKind.PER_FN, label=f"per-fn {len(units)} stub(s), 0 re-fill(s)",
        targets=frozenset(units),
        target_tests=frozenset().union(*(t for _, t in tests)),
        stubs_in_targets=frozenset(units),
        files=tuple(sorted({u.file for u in units})), unit_tests=tests)


def _fill(tmp_path: Path, routes_of: Any, *, sweep_held_holder: bool = False,
          doc: bool = False) -> tuple[list[str], list[str], list[str]]:
    fill_reading.reset_last_comparison()
    tree = _Tree(doc)
    calls: list[str] = []
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    hollow = {_hname(c, doc): frozenset({c}) for c in _CALLERS}
    if sweep_held_holder:
        # The sweep already read a test stopping in the holder: it is in
        # the plan's held set (`planned_held`).
        hollow["t_sweep"] = frozenset({_HOLDER})
    fs.execute_fill(
        routes_of(calls, tree), hollow=hollow,
        bodies_sha=lambda _t: f"b{len(tree.kept)}",
        deps=fs.FillExecutionDeps(
            log=logs.append,
            emit=lambda name, **k: events.append((name, k.get("data") or {})),
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=1, oracle_fp="o",
            premise=tree.premise(), ground_now=tree.premise,
            held_now=tree.held))
    opened = [d.get("into", "") for n, d in events
              if n == "phase_c.fill.blocker_opened"]
    return calls, opened, logs


def _callers_only(takes: bool = True) -> Any:
    return lambda calls, tree: [
        _PerUnit(_per_fn_run(_CALLERS), calls, tree, takes=takes)]


def test_the_holder_is_claimed_before_the_next_caller_of_the_run_in_flight(
        tmp_path: Path) -> None:
    calls, opened, _logs = _fill(tmp_path, _callers_only())
    assert calls == ["c1", "h", "c2", "c3"]
    assert opened == ["per-fn 3 stub(s), 0 re-fill(s)"]


def test_negative_control_the_boundary_opener_lets_the_run_finish_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-change reaction: holders taken only between waves (the
    boundary opener), never at a run's claim — the 15370 order."""
    taken = fs._GroundGate._take_holders

    def _between_waves_only(self, units, run, holders):  # type: ignore[no-untyped-def]
        if run is None:
            taken(self, units, run, holders)

    monkeypatch.setattr(fs._GroundGate, "_take_holders", _between_waves_only)
    calls, opened, _logs = _fill(tmp_path, _callers_only())
    assert calls == ["c1", "c2", "c3", "h"]
    assert opened == [""]


def test_a_run_that_cannot_take_it_waits_behind_the_run_opened_on_it(
        tmp_path: Path) -> None:
    calls, opened, logs = _fill(tmp_path, _callers_only(takes=False))
    assert calls == ["c1", "h", "c2", "c3"]
    assert opened == [""]
    assert any("waits for per-fn 1 blocker(s)" in m for m in logs)


@pytest.mark.parametrize("doc", [False, True])
@pytest.mark.parametrize("sweep_held", [False, True])
def test_a_holder_that_is_a_later_target_of_the_same_run_comes_forward(
        tmp_path: Path, sweep_held: bool, doc: bool) -> None:
    """The review: the current wave's targets counted as ahead, so a holder
    the run in flight would write last was not pulled forward; the second
    review: a holder the sweep already held was excluded from the take as
    the plan's to place; the third: a doc test's moved key (`#k`) and held
    key (`(line N)`) were compared raw and never joined."""
    def routes(takes: bool) -> Any:
        return lambda calls, tree: [
            _PerUnit(_per_fn_run((*_CALLERS, _HOLDER), doc), calls, tree,
                     takes=takes)]

    calls, opened, _logs = _fill(tmp_path, routes(True),
                                 sweep_held_holder=sweep_held, doc=doc)
    assert calls == ["c1", "h", "c2", "c3"]
    assert len(opened) == 1
    calls, opened, _logs = _fill(tmp_path / "control", routes(False),
                                 sweep_held_holder=sweep_held, doc=doc)
    assert calls == ["c1", "c2", "c3", "h"]
    assert opened == []


def test_negative_control_doc_test_names_compared_raw_do_not_join(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The join before the third review: the held view keyed raw, the moved
    set split on `::` only."""
    from pipeline_core.domain.test_outcome import held_locators
    monkeypatch.setattr(fs, "test_leaf", lambda n: n.rsplit("::", 1)[-1])
    monkeypatch.setattr(fs, "held_by_leaf", lambda h: dict(held_locators(h)))
    calls, opened, _logs = _fill(
        tmp_path, lambda calls, tree: [
            _PerUnit(_per_fn_run((*_CALLERS, _HOLDER), True), calls, tree)],
        sweep_held_holder=True, doc=True)
    assert calls == ["c1", "c2", "c3", "h"]
    assert opened == []


def test_an_own_target_a_run_ahead_aims_at_stays_that_runs(
        tmp_path: Path) -> None:
    """The third review: taking an own target a later run also aims at
    leaves that run to have its body withheld. The holder stays where the
    plan put it in the asking run; the later run is promoted."""
    file_h = RouteRun(kind=RouteKind.FILE, label="file src/p.rs",
                      targets=frozenset({_HOLDER}),
                      target_tests=frozenset({"t_c1"}),
                      stubs_in_targets=frozenset({_HOLDER}),
                      files=("src/p.rs",))

    def routes(calls: list[str], tree: _Tree) -> list:
        w = _Whole([file_h], calls, tree)
        w.parallel = False
        return [_PerUnit(_per_fn_run((*_CALLERS, _HOLDER)), calls, tree), w]

    calls, opened, logs = _fill(tmp_path, routes, sweep_held_holder=True)
    assert calls[:4] == ["c1", "c2", "c3", "h"]
    assert opened == []
    assert not any("taken into" in m for m in logs)


def test_negative_control_without_own_targets_a_sweep_held_holder_stays_last(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicate before the second review: the asking run's own targets
    were not exempt from the sweep's held set."""
    real = fs.unwritten_holders
    monkeypatch.setattr(fs, "unwritten_holders",
                        lambda *a, **k: real(*a, **{**k, "own": ()}))
    calls, opened, _logs = _fill(
        tmp_path, lambda calls, tree: [
            _PerUnit(_per_fn_run((*_CALLERS, _HOLDER)), calls, tree)],
        sweep_held_holder=True)
    assert calls == ["c1", "c2", "c3", "h"]
    assert opened == []


def test_a_sweep_held_holder_the_plan_left_out_of_the_run_stays_out(
        tmp_path: Path) -> None:
    """Exempting the run's own targets does not reach past them: a held stub
    the plan placed in no run (cut by the cap, exhausted) is not taken."""
    calls, opened, _logs = _fill(tmp_path, _callers_only(),
                                 sweep_held_holder=True)
    assert calls == ["c1", "c2", "c3"]
    assert opened == []


def test_a_holder_no_run_would_take_makes_no_claim_wait(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate reads one predicate: when it names nothing (asked under
    unmoved evidence, taken already, …) every claim goes on."""
    monkeypatch.setattr(fs, "unwritten_holders", lambda *_a, **_k: [])
    calls, opened, _logs = _fill(tmp_path, _callers_only())
    assert calls == ["c1", "c2", "c3"]
    assert opened == []


def test_a_run_of_another_route_has_the_holder_opened_at_its_claim(
        tmp_path: Path) -> None:
    """A file wave cannot take a unit it was not planned with: the claim
    after the keep opens the per-fn run at the cursor, before the wave's
    rows are written — the decision is the claim's, not the boundary's —
    and that claim waits behind it, so the holder is written first."""
    first = RouteRun(kind=RouteKind.FILE, label="file src/a.rs",
                     targets=frozenset({_CALLERS[0]}),
                     target_tests=frozenset({"t_c1"}),
                     stubs_in_targets=frozenset({_CALLERS[0]}),
                     files=("src/a.rs",))
    second = RouteRun(kind=RouteKind.FILE, label="file src/b.rs",
                      targets=frozenset({_CALLERS[1]}),
                      target_tests=frozenset({"t_c2"}),
                      stubs_in_targets=frozenset({_CALLERS[1]}),
                      files=("src/b.rs",))

    def routes(calls: list[str], tree: _Tree) -> list:
        return [_Whole([first, second], calls, tree),
                _PerUnit(None, calls, tree)]

    calls, opened, logs = _fill(tmp_path, routes)
    assert calls == ["file src/a.rs", "h", "file src/b.rs"]
    assert opened == [""]
    at_claim = next(n for n, m in enumerate(logs) if "opened at the cursor" in m)
    first_row = next(n for n, m in enumerate(logs)
                     if "[phase-c/route] file src/a.rs" in m)
    assert at_claim < first_row


def test_no_reader_of_the_held_view_outside_the_gate() -> None:
    """The review's check: every reaction to a keep's measurement is the
    claim gate's, so nothing in the executor reads `gate.held` (the
    boundary opener and requeue did)."""
    tree = ast.parse(inspect.getsource(fs))
    readers = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("held", "blockers_now")
        and isinstance(node.value, ast.Name) and node.value.id == "gate"]
    assert readers == []
    assert not hasattr(fs, "_requeue_behind")
    assert not hasattr(fs, "_open_for_blockers")


# ---- the clock's answer ------------------------------------------------------

def test_the_clock_answers_take_until_the_holder_is_drained_or_claimed() -> None:
    q = HolderQueue()
    clock = RunClock(share_minutes=None, fill_remaining=lambda: None,
                     holders=q, yields_to=lambda _s, _u: "")
    q.admit(HolderAdmission(frozenset({_HOLDER}),
                            ((_HOLDER_SITE, frozenset({"t_c1"})),)))
    assert clock.refusal(None, [_CALLERS[1]]) == "take"
    taken = clock.take_holders()
    assert [a.units for a in taken] == [frozenset({_HOLDER})]
    assert _HOLDER in clock.owed
    assert clock.refusal(None, [_CALLERS[1]]) == ""
    # A claim that writes the holder itself takes it off the front.
    q.admit(HolderAdmission(frozenset({_HOLDER}),
                            ((_HOLDER_SITE, frozenset({"t_c1"})),)))
    assert clock.refusal(None, [_HOLDER]) == ""
    assert clock.take_holders() == []
    assert q.claimed() >= {_CALLERS[1], _HOLDER}


# ---- the engine's pool -------------------------------------------------------

class _Recorder:
    """The per-fn unit: records each body it is asked to write and the tests
    its context judges that body on."""

    def __init__(self, *, block_second: threading.Event | None = None) -> None:
        self.order: list[str] = []
        self.judged: dict[str, set[str]] = {}
        self.lock = threading.Lock()
        self.c1_done = threading.Event()
        self._block = block_second

    def run(self, _idx: int, rec: Any, *, ctx: Any) -> FillResult:
        if rec.fn_name == "c2" and self._block is not None:
            self.c1_done.wait(timeout=5)
        with self.lock:
            self.order.append(rec.fn_name)
            front = ctx.tdd_frontier_tests
            self.judged[rec.fn_name] = (
                set(front.convicting_for_body(rec.file, rec.fn_name, rec.container))
                if front is not None else set())
        if rec.fn_name == "c1":
            self.c1_done.set()
        return FillResult(file=rec.file, fn_name=rec.fn_name, filled=True,
                          attempts=1)


def _pass(tmp_path: Path, *, holders: HolderQueue | None,
          frontier_keys: set[FnLocator], parallelism: int = 1,
          block: bool = False) -> _Recorder:
    from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
    from pipeline_core.domain.enums import FillUnit
    from tests.test_fill_engine_usecases import FakeFill, _engine, _mk_ws

    ws = _mk_ws(tmp_path)
    recs = [{"file": u.file, "fn_name": u.fn_name, "container": "",
             "sig_text": f"pub fn {u.fn_name}()"} for u in (*_CALLERS, _HOLDER)]
    fake = FakeFill(stub_records=recs, run=(0, ""), regr=None)
    recorder = _Recorder(block_second=threading.Event() if block else None)
    base = _engine(fake, FillUnit.FN)
    eng = replace(base, fill_one=recorder,  # type: ignore[arg-type]
                  config=replace(base.config, parallelism=parallelism))
    admitted: list[bool] = []

    def _gate(_scope: Any, units: frozenset[FnLocator]) -> str:
        # The claim gate's move after the keep of c1: hand the holder over.
        if (holders is not None and recorder.c1_done.is_set() and not admitted
                and _HOLDER not in units):
            admitted.append(True)
            holders.admit(HolderAdmission(
                frozenset({_HOLDER}), ((_HOLDER_SITE, frozenset({"t_c1"})),)))
        return ""

    clock = RunClock(share_minutes=None, fill_remaining=lambda: None,
                     yields_to=_gate, holders=holders)
    eng._per_fn_passes(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, max_passes=1,
        leaf_name_counts=None, fr_path=None,
        tdd_frontier_keys=frontier_keys, skip_end_pass_test=True,
        tdd_frontier_tests=FailureFrontier.build({}, {}),
        tdd_test_cmd=None, crash_evidence=None, crash_blamed=None,
        crash_blocked=None, culprits_of=None, dispatch_ranks=None,
        behaviour_specs=(), _cfg_dead=CfgDeadSites(),
        _is_cfg_dead=lambda _k: False, _dispatched=set(),
        _reverted=set(), _answer_lost=set(), landed_before=lambda: 0,
        claim=clock.may_claim, claim_refusal=clock.refusal,
        claim_release=clock.release, take_holders=clock.take_holders)
    return recorder


def test_the_pool_writes_a_handed_holder_before_its_next_unit(
        tmp_path: Path) -> None:
    got = _pass(tmp_path, holders=HolderQueue(), frontier_keys=set(_CALLERS))
    assert got.order == ["c1", "h", "c2", "c3"]
    # Judged on the tests the fill measured in it, not the sweep's frontier.
    assert got.judged["h"] == {"t_c1"}


def test_negative_control_a_pool_with_no_holder_queue_never_writes_it(
        tmp_path: Path) -> None:
    got = _pass(tmp_path, holders=None, frontier_keys=set(_CALLERS))
    assert got.order == ["c1", "c2", "c3"]


def test_a_handed_holder_queued_in_the_pass_is_written_once(
        tmp_path: Path) -> None:
    got = _pass(tmp_path, holders=HolderQueue(),
                frontier_keys={*_CALLERS, _HOLDER})
    assert got.order.count("h") == 1
    assert got.order.index("h") < got.order.index("c2")


def test_with_two_workers_the_holder_precedes_the_unit_claimed_after_the_keep(
        tmp_path: Path) -> None:
    """Pool width 2: c1 and c2 are claimed together; c2 cannot finish
    before c1's keep, so the next claim after that keep (c3's, on either
    worker) is answered "take" and the holder is written before c3."""
    got = _pass(tmp_path, holders=HolderQueue(), frontier_keys=set(_CALLERS),
                parallelism=2, block=True)
    assert sorted(got.order) == ["c1", "c2", "c3", "h"]
    assert got.order.index("h") < got.order.index("c3")


# ---- the requeue bound -------------------------------------------------------

def test_two_runs_waiting_on_each_other_each_wait_once_and_both_run(
        tmp_path: Path) -> None:
    """The wait is bounded by (run, writer): a run's claims wait behind a
    given writer once per fill. The bound it replaces was a count of two
    per planned run, shared by every run carved from it, so each layer an
    opened run yielded on spent the planned run's count. Here two file runs
    each hold the other's test: each waits once, then both run."""
    a, b, x = (FnLocator("src/a.rs", "a"), FnLocator("src/b.rs", "b"),
               FnLocator("src/x.rs", "x"))

    def premise() -> dict:
        return {"t_a": (False, ("fn", b.file, b.fn_name, ""), False),
                "t_b": (False, ("fn", a.file, a.fn_name, ""), False)}

    def held() -> dict[str, frozenset[FnSite]]:
        return {"t_a": frozenset({FnSite(b.file, b.fn_name, "")}),
                "t_b": frozenset({FnSite(a.file, a.fn_name, "")})}

    def file_run(u: FnLocator, tests: frozenset[str]) -> RouteRun:
        return RouteRun(kind=RouteKind.FILE, label=f"file {u.file}",
                        targets=frozenset({u}), target_tests=tests,
                        stubs_in_targets=frozenset({u}), files=(u.file,))

    calls: list[str] = []

    class _Serial(_Whole):
        def __init__(self, runs: Sequence[RouteRun]) -> None:
            super().__init__(runs, calls, _Tree())
            self.parallel = False

    fill_reading.reset_last_comparison()
    logs: list[str] = []
    fs.execute_fill(
        [_Serial([file_run(x, frozenset()), file_run(a, frozenset({"t_a"})),
                  file_run(b, frozenset({"t_b"}))])],
        hollow={}, bodies_sha=lambda _t: str(len(calls)),
        deps=fs.FillExecutionDeps(
            log=logs.append, emit=lambda *_a, **_k: None,
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=1, oracle_fp="o",
            premise=premise(), ground_now=premise, held_now=held))
    assert sorted(calls) == ["file src/a.rs", "file src/b.rs", "file src/x.rs"]
    assert calls[0] == "file src/x.rs"
    assert sum("waits behind" in m for m in logs) == 2
