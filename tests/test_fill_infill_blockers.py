"""Blockers-first, applied again inside the fill to the blockers its own
keeps measured.

Run `42057-641e589c` (2026-10-26), fill 3: the per-fn run opened first on
the sweep's blockers and spent 96.5 min and 20 calls keeping 12 bodies, each
logged `kept … blocked by still-stubbed sibling(s)` naming
`src/ptr.rs::from_raw` or `src/error.rs::object_drop` from its third keep
on; both were targets of the file runs ranked 2nd and 4th. Plan sentence: a
run that writes a now-blocker opens before the runs whose remaining claims'
tests are held by it, and the held claims wait behind it, not refused.

NEGATIVE CONTROL: with no held view (the plan-time order only) the per-fn
run's remaining claims are refused on premise in front of the writer, and
the writer opens after them.

Invented shape, no crate names.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.application.reconstruct import fill_scheduler as fs
from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, RunClock
from pipeline_core.domain.models import FnLocator

_X = FnLocator("src/a.rs", "base")      # held at plan time; the per-fn run writes it
_C1 = FnLocator("src/a.rs", "use_one")
_C2 = FnLocator("src/a.rs", "use_two")
_S = FnLocator("src/b.rs", "deep")      # the blocker a keep measures; the file run writes it


def _fn(fn: FnLocator) -> tuple[str, ...]:
    return ("fn", fn.file, fn.fn_name, "")


class _Tree:
    """What has been written, and the ground each state reads."""

    def __init__(self) -> None:
        self.kept: set[FnLocator] = set()

    def premise(self) -> dict:
        if _S in self.kept:
            return {"t0": (True, (), False), "t1": (False, _fn(_C1), False),
                    "t2": (False, _fn(_C2), False)}
        if _X in self.kept:
            return {"t0": (True, (), False), "t1": (False, _fn(_S), False),
                    "t2": (False, _fn(_S), False)}
        return {t: (False, _fn(_X), False) for t in ("t0", "t1", "t2")}

    def held(self) -> dict[str, frozenset[FnLocator]]:
        held: dict[str, frozenset[FnLocator]] = {}
        for t, (passed, site, _a) in self.premise().items():
            if passed or len(site) < 3:
                continue
            loc = FnLocator(site[1], site[2])
            if loc in (_X, _S) and loc not in self.kept:
                held[t] = frozenset({loc})
        return held


class _PerUnit:
    """A route that claims and writes each target on its own."""

    def __init__(self, run: RouteRun, calls: list[str], tree: _Tree) -> None:
        self.kind = run.kind
        self.parallel = False
        self._run = run
        self._calls = calls
        self._tree = tree

    def plan(self) -> Sequence[RouteRun]:
        return [self._run]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        kept: set[FnLocator] = set()
        for unit, tests in run.unit_tests:
            if clock.refusal(tests, [unit]):
                continue
            self._calls.append(f"{unit.fn_name}")
            self._tree.kept.add(unit)
            fill_reading.note_kept(0)
            kept.add(unit)
        return RouteRunReport(model_calls=len(kept), kept=frozenset(kept))


class _Whole:
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


def _fill(tmp_path: Path, *, held: bool) -> tuple[list[str], fs.FillExecution]:
    fill_reading.reset_last_comparison()
    tree = _Tree()
    per_fn = RouteRun(
        kind=RouteKind.PER_FN, label="per-fn 1 stub(s), 2 re-fill(s)",
        targets=frozenset({_X, _C1, _C2}),
        target_tests=frozenset({"t0", "t1", "t2"}),
        stubs_in_targets=frozenset({_X}), files=("src/a.rs",),
        unit_tests=((_X, frozenset({"t0"})), (_C1, frozenset({"t1"})),
                    (_C2, frozenset({"t2"}))))
    file_run = RouteRun(kind=RouteKind.FILE, label="file src/b.rs",
                        targets=frozenset({_S}),
                        stubs_in_targets=frozenset({_S}), files=("src/b.rs",))
    calls: list[str] = []
    out = fs.execute_fill(
        [_PerUnit(per_fn, calls, tree), _Whole(file_run, calls, tree)],
        hollow={t: frozenset({_X}) for t in ("t0", "t1", "t2")},
        bodies_sha=lambda _t: f"b{len(tree.kept)}",
        deps=fs.FillExecutionDeps(
            log=lambda _m: None, emit=lambda *_a, **_k: None,
            check_stop=lambda _c: None, ledger=RouteLedgerStore(tmp_path),
            project="p", cluster="c", run_id="r", iteration=3, oracle_fp="o",
            premise=tree.premise(), ground_now=tree.premise,
            held_now=tree.held if held else None))
    return calls, out


def test_the_claims_held_by_a_measured_blocker_wait_behind_its_writer(
        tmp_path: Path) -> None:
    calls, _out = _fill(tmp_path, held=True)
    assert calls == ["base", "file src/b.rs", "use_one", "use_two"]


def test_negative_control_the_plan_time_order_refuses_them_in_front(
        tmp_path: Path) -> None:
    calls, _out = _fill(tmp_path, held=False)
    assert calls == ["base", "file src/b.rs"]
