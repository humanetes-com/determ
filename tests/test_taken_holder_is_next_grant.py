"""A holder a run took is the next grant of that run's clock.

Run `1878910-be648607` (2026-10-31), t2a fill 1: the claim gate handed the
per-fn run a measured holder at 08:27:12 and the worker that asked took it.
The holder's own claim then waited at the reserved share beside the run's
plan units; every release woke every waiter, three plan units won the race,
and a second holder taken by the waiting claim was written first. The
holder started 10.7 minutes and four units after it was taken.

Plan sentence: while a taken holder's claim waits, or the thread that took
holders is between their claims, no other claim of the run is granted;
taken holders are granted in take order, and the claim of one is never
answered "take". A taken holder the route never claims, or a thread that
ends, holds nothing.

NEGATIVE CONTROLS: the clock with the line removed (no drain mark, no take
order), which is the clock before this change: a plan claim is granted
between two holders taken together, and a waiting holder's claim is
answered "take" and writes the newer holder first.

Invented shape, no crate names.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain import fill_schedule
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.fill_schedule import HolderAdmission, HolderQueue, RunClock
from pipeline_core.domain.models import FillResult, FnLocator, FnSite

_P = tuple(FnLocator("src/a.rs", f"p{n}") for n in range(1, 5))
_H1 = FnLocator("src/h.rs", "h1")
_H2 = FnLocator("src/h.rs", "h2")


def _adm(*units: FnLocator) -> HolderAdmission:
    return HolderAdmission(frozenset(units), tuple(
        (FnSite(u.file, u.fn_name, ""), frozenset({f"t_{u.fn_name}"})) for u in units))


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fill_schedule, "_RESERVED_WAIT_POLL_S", 0.02)


def _clock(q: HolderQueue, ground: Callable[..., bool] | None = None) -> RunClock:
    """A 13-minute share with no price and a clock that does not move: the
    first grant reserves the rest of the share, so the run is serial and
    every other claim waits for a release (fill 1 of the recorded run)."""
    return RunClock(share_minutes=13.0, fill_remaining=lambda: 100.0,
                    now=lambda: 0.0, holders=q, yields_to=lambda _s, _u: "",
                    ground_moved=ground)


class _Worker:
    """A thread that runs a script of claims, each released on its own go."""

    def __init__(self, clock: RunClock, granted: list[str]) -> None:
        self.clock = clock
        self.granted = granted
        self.answers: list[str] = []
        self.go: dict[str, threading.Event] = {}
        self.thread: threading.Thread | None = None

    def claim(self, unit: FnLocator, *, hold: bool = True) -> str:
        why = self.clock.refusal(None, [unit])
        self.answers.append(why or unit.fn_name)
        if why:
            return why
        self.granted.append(unit.fn_name)
        if hold:
            self.go.setdefault(unit.fn_name, threading.Event()).wait(5)
        self.clock.release()
        return ""

    def start(self, script: Callable[[_Worker], None]) -> _Worker:
        self.thread = threading.Thread(target=script, args=(self,), daemon=True)
        self.thread.start()
        return self

    def release(self, name: str) -> None:
        self.go.setdefault(name, threading.Event()).set()


def _pause(seconds: float) -> None:
    """A real wait: the suite's conftest makes `time.sleep` a no-op."""
    threading.Event().wait(seconds)


def _until(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        _pause(0.005)
    return cond()


def _waiting(w: _Worker, n: int) -> bool:
    """`w` has made `n` answers and is inside its next claim."""
    _pause(0.1)
    return len(w.answers) == n and w.thread is not None and w.thread.is_alive()


def test_a_waiting_holder_is_granted_before_a_plan_unit_that_asked_with_it() -> None:
    q = HolderQueue()
    clock = _clock(q)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1))

    def b_script(w: _Worker) -> None:
        assert w.clock.refusal(None, [_P[1]]) == "take"
        w.clock.take_holders()
        w.claim(_H1)
    b = _Worker(clock, granted).start(b_script)
    assert _until(lambda: bool(clock._tickets))
    c = _Worker(clock, granted).start(lambda w: w.claim(_P[2], hold=False))
    assert _waiting(c, 0)
    a.release("p1")
    assert _until(lambda: granted == ["p1", "h1"])
    assert _waiting(c, 0)          # the holder holds the share now
    b.release("h1")
    assert _until(lambda: granted == ["p1", "h1", "p3"])


def test_a_holder_refused_on_its_premise_frees_the_line() -> None:
    moved = threading.Event()
    q = HolderQueue()
    clock = _clock(q, ground=lambda _s, units: moved.is_set() and _H1 in units)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1))

    def b_script(w: _Worker) -> None:
        w.clock.refusal(None, [_P[1]])
        w.clock.take_holders()
        w.claim(_H1)               # refused; the thread then ends
    b = _Worker(clock, granted).start(b_script)
    assert _until(lambda: bool(clock._tickets))
    c = _Worker(clock, granted).start(lambda w: w.claim(_P[2], hold=False))
    assert _waiting(c, 0)
    moved.set()
    a.release("p1")
    assert _until(lambda: granted == ["p1", "p3"])
    assert b.answers == ["premise"]


def _two_taken(clear_line: bool) -> tuple[list[str], _Worker]:
    """Two holders taken together; the worker that took them releases the
    first and only then claims the second, while a plan claim waits."""
    q = HolderQueue()
    clock = _clock(q)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1, _H2))
    between = threading.Event()
    go_on = threading.Event()
    took = threading.Event()

    def b_script(w: _Worker) -> None:
        w.clock.refusal(None, [_P[1]])
        w.clock.take_holders()
        took.set()
        if clear_line:             # the clock before this change: no line
            with w.clock._lock:
                w.clock._draining.clear()
                w.clock._take_order.clear()
        w.claim(_H1)
        between.set()              # h1 returned; h2 not yet claimed
        go_on.wait(5)
        w.claim(_H2, hold=False)
        w.claim(_P[1], hold=False)
    b = _Worker(clock, granted).start(b_script)
    assert took.wait(3)
    _pause(0.1)                # b is inside its claim of h1
    c = _Worker(clock, granted).start(lambda w: w.claim(_P[2], hold=False))
    assert _waiting(c, 0)
    a.release("p1")
    assert _until(lambda: "h1" in granted)
    b.release("h1")
    assert between.wait(3)
    # The plan claim's chance: it lands now or never (the line holds it).
    _until(lambda: "p3" in granted, timeout=1.0)
    go_on.set()
    assert _until(lambda: len(granted) == 5)
    return granted, c


def test_two_holders_taken_together_are_both_granted_before_a_plan_unit() -> None:
    granted, _c = _two_taken(clear_line=False)
    assert granted[:3] == ["p1", "h1", "h2"]


def test_negative_control_without_the_line_a_plan_unit_slips_between_them() -> None:
    granted, _c = _two_taken(clear_line=True)
    # The plan claim wins the first release or the gap after h1's return.
    assert granted.index("p3") < granted.index("h2")


def _nested(clear_line: bool) -> tuple[list[str], list[str]]:
    """A holder's claim is waiting when the gate hands the run a second."""
    q = HolderQueue()
    clock = _clock(q)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1))

    def b_script(w: _Worker) -> None:
        w.clock.refusal(None, [_P[1]])
        w.clock.take_holders()
        if clear_line:
            with w.clock._lock:
                w.clock._draining.clear()
                w.clock._take_order.clear()
        while w.claim(_H1, hold=False) == "take":
            w.clock.take_holders()
            w.claim(_H2, hold=False)
    b = _Worker(clock, granted).start(b_script)
    assert _until(lambda: len(b.answers) == 0 and b.thread is not None)
    _pause(0.1)
    q.admit(_adm(_H2))
    # The waiting holder re-asks first: answered "take" or never.
    _until(lambda: "take" in b.answers, timeout=1.0)

    def c_script(w: _Worker) -> None:
        while w.claim(_P[2], hold=False) == "take":
            w.clock.take_holders()
            w.claim(_H2, hold=False)
    _Worker(clock, granted).start(c_script)
    _pause(0.2)
    a.release("p1")
    assert _until(lambda: "h1" in granted and "h2" in granted)
    return granted, b.answers


def test_a_waiting_holder_is_not_answered_take_and_holders_go_in_take_order() -> None:
    granted, b_answers = _nested(clear_line=False)
    assert "take" not in b_answers
    assert granted.index("h1") < granted.index("h2")


def test_negative_control_without_the_line_the_waiting_holder_takes_the_next() -> None:
    granted, b_answers = _nested(clear_line=True)
    assert b_answers[0] == "take"
    assert granted.index("h2") < granted.index("h1")


def test_a_holder_its_thread_never_claims_does_not_hold_the_run() -> None:
    """The route passes over a taken holder (written by another worker, or
    not a stub of the pass) and claims its own unit next: the line is free.
    A thread that ends after its take holds nothing either."""
    for ends in (False, True):
        q = HolderQueue()
        clock = _clock(q)
        granted: list[str] = []
        a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
        assert _until(lambda g=granted: g == ["p1"])
        q.admit(_adm(_H1))
        took = threading.Event()
        go_on = threading.Event()

        def b_script(w: _Worker, ends: bool = ends,
                     took: threading.Event = took,
                     go_on: threading.Event = go_on) -> None:
            w.clock.refusal(None, [_P[1]])
            w.clock.take_holders()
            took.set()
            go_on.wait(5)
            if not ends:
                w.claim(_P[1], hold=False)
        b = _Worker(clock, granted).start(b_script)
        assert took.wait(3)
        c = _Worker(clock, granted).start(lambda w: w.claim(_P[2], hold=False))
        assert _waiting(c, 0)
        go_on.set()
        a.release("p1")
        assert _until(lambda g=granted: "p3" in g)
        assert "h1" not in granted
        assert b.thread is not None


# ---- the engine's pool, the recorded shape ----------------------------------

class _Recorder:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.p1_done = threading.Event()

    def run(self, _idx: int, rec: Any, *, ctx: Any) -> FillResult:
        with self.lock:
            self.order.append(rec.fn_name)
        _pause(0.05)
        if rec.fn_name == "p1":
            self.p1_done.set()
        return FillResult(file=rec.file, fn_name=rec.fn_name, filled=True,
                          attempts=1)


def test_on_the_real_pool_width_four_no_price_the_holders_are_the_next_starts(
        tmp_path: Path) -> None:
    """Four workers, a share with no price: p1 is granted and reserves the
    share, p2..p4 wait. p1's keep measures two holders; the next claim takes
    them. Both are written before any other plan unit."""
    from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
    from pipeline_core.domain.enums import FillUnit
    from tests.test_fill_engine_usecases import FakeFill, _engine, _mk_ws

    ws = _mk_ws(tmp_path)
    recs = [{"file": u.file, "fn_name": u.fn_name, "container": "",
             "sig_text": f"pub fn {u.fn_name}()"} for u in (*_P, _H1, _H2)]
    fake = FakeFill(stub_records=recs, run=(0, ""), regr=None)
    recorder = _Recorder()
    base = _engine(fake, FillUnit.FN)
    eng = replace(base, fill_one=recorder,  # type: ignore[arg-type]
                  config=replace(base.config, parallelism=4))
    q = HolderQueue()
    admitted: list[bool] = []
    lock = threading.Lock()

    def _gate(_scope: Any, units: frozenset[FnLocator]) -> str:
        with lock:
            if recorder.p1_done.is_set() and not admitted:
                admitted.append(True)
                q.admit(_adm(_H1, _H2))
        return ""

    clock = RunClock(share_minutes=13.0, fill_remaining=lambda: 100.0,
                     now=lambda: 0.0, yields_to=_gate, holders=q)
    eng._per_fn_passes(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, max_passes=1,
        leaf_name_counts=None, fr_path=None,
        tdd_frontier_keys=set(_P), skip_end_pass_test=True,
        tdd_frontier_tests=FailureFrontier.build({}, {}),
        tdd_test_cmd=None, crash_evidence=None, crash_blamed=None,
        crash_blocked=None, culprits_of=None, dispatch_ranks=None,
        behaviour_specs=(), _cfg_dead=CfgDeadSites(),
        _is_cfg_dead=lambda _k: False, _dispatched=set(),
        _reverted=set(), _answer_lost=set(), landed_before=lambda: 0,
        claim=clock.may_claim, claim_refusal=clock.refusal,
        claim_release=clock.release, take_holders=clock.take_holders)
    assert recorder.order[0] == "p1"
    assert recorder.order[1:3] == ["h1", "h2"]
    assert sorted(recorder.order[3:]) == ["p2", "p3", "p4"]
