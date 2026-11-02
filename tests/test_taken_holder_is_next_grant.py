"""A holder a run took is the next grant of that run's clock.

Run `1878910-be648607` (2026-10-31), t2a fill 1: the claim gate handed the
per-fn run a measured holder at 08:27:12 and the worker that asked took it.
The holder's own claim then waited at the reserved share beside the run's
plan units; every release woke every waiter, three plan units won the race,
and a second holder taken by the waiting claim was written first. The
holder started 10.7 minutes and four units after it was taken.

Plan sentence: while a taken holder's claim waits, or the thread that took
a holder has not yet claimed it, no other claim of the run is granted;
taken holders are granted in take order, and the claim of one is never
answered "take". A taken holder the route never claims, or a thread that
ends, holds nothing.

Run `3178980-c42f7944` (2026-11-01), t2a fill 1, narrowed the line: the
thread that took holders held it until every holder it took had returned,
so the four-wide pool wrote one unit at a time and a holder taken with
another waited 20 minutes on the same worker. A take is one body; a holder
still at the front is taken by the next claim of any worker; a granted
holder does not hold the line while it runs.

NEGATIVE CONTROLS: the clock with the line removed: a waiting holder's
claim is answered "take" and writes the newer holder first. The queue that
handed one thread every waiting holder: a plan claim lands between two
holders taken together, and the second holder waits for the first to
return although the share has room for both.

Run `528728-f4e7de82` (2026-11-02), t2a fill 1: a pool worker whose plan
queue was empty returned, so holders admitted together ran one after
another on the workers still inside a claim (two of four had left by
20:13). And a keep admits nothing: the gate admits at a claim, so a holder
measured by a worker that claims nothing after its keep waited for another
worker's unit to return (21:28:59 -> 21:31:30). Plan sentences: a worker
of a holder-taking run stays a claimer until the front is empty and no
worker of the level is inside a claim; an idle worker asks the gate itself;
a holder measured in a level is written before the level joins.

NEGATIVE CONTROLS (pool): the pool with no claimer state (the queue not
passed: workers return on an empty plan queue), and the pool with the
claimer state but no gate ask (the plan before its review): in both the
measured holders do not start while the blocked plan units stay blocked.

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


def _taking(w: _Worker, unit: FnLocator, *, hold: bool = False) -> str:
    """One claim as the per-fn worker makes it: answered "take", write the
    front holder (one body) and ask again."""
    while (why := w.clock.refusal(None, [unit])) == "take":
        w.answers.append("take")
        for adm in w.clock.take_holders():
            for site, _t in adm.held_tests:
                w.claim(FnLocator(site.file, site.fn_name), hold=hold)
    w.answers.append(why or unit.fn_name)
    if why:
        return why
    w.granted.append(unit.fn_name)
    if hold:
        w.go.setdefault(unit.fn_name, threading.Event()).wait(5)
    w.clock.release()
    return ""


def _two_taken(take_all: bool) -> list[str]:
    """Two holders admitted together while a plan unit holds the only
    reservation. The worker that took first pauses between its holder's
    return and its next claim; a plan claim waits."""
    q = HolderQueue()
    if take_all:                   # the queue before this change
        def _all() -> list[HolderAdmission]:
            with q._lock:
                out, q._pending = q._pending, []
                return out
        q.take = _all              # type: ignore[method-assign]
    clock = _clock(q)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1, _H2))
    between = threading.Event()
    go_on = threading.Event()
    took = threading.Event()

    def b_script(w: _Worker) -> None:
        assert w.clock.refusal(None, [_P[1]]) == "take"
        taken = w.clock.take_holders()
        took.set()
        w.claim(_H1)
        between.set()              # h1 returned; b's next claim not made yet
        go_on.wait(5)
        if len(taken[0].held_tests) > 1:
            w.claim(_H2, hold=False)
        _taking(w, _P[1])
    b = _Worker(clock, granted).start(b_script)
    assert took.wait(3)
    _pause(0.1)                # b is inside its claim of h1
    c = _Worker(clock, granted).start(lambda w: _taking(w, _P[2]))
    _pause(0.1)
    a.release("p1")
    assert _until(lambda: "h1" in granted)
    b.release("h1")
    assert between.wait(3)
    # The plan claim's chance: it lands now or never (the front holds it).
    _until(lambda: "p3" in granted, timeout=1.0)
    go_on.set()
    assert _until(lambda: len(granted) == 5)
    assert c.thread is not None
    return granted


def test_two_holders_taken_together_are_both_granted_before_a_plan_unit() -> None:
    granted = _two_taken(take_all=False)
    assert granted[:3] == ["p1", "h1", "h2"]


def test_negative_control_one_thread_taking_both_lets_a_plan_unit_between() -> None:
    granted = _two_taken(take_all=True)
    # Both bodies sit on b's thread; the line ends at b's claim of h1, and
    # the plan claim lands in the gap before b claims h2.
    assert granted.index("p3") < granted.index("h2")


def _side_by_side(take_all: bool) -> tuple[list[str], bool]:
    """A price that fits three calls in the share: p1 runs, two holders are
    admitted together, b takes and is granted the first and holds it. Is
    the second granted while the first still runs?"""
    q = HolderQueue()
    if take_all:
        def _all() -> list[HolderAdmission]:
            with q._lock:
                out, q._pending = q._pending, []
                return out
        q.take = _all              # type: ignore[method-assign]
    clock = RunClock(share_minutes=13.0, fill_remaining=lambda: 100.0,
                     now=lambda: 0.0, holders=q, yields_to=lambda _s, _u: "",
                     call_price=4.0)
    granted: list[str] = []
    a = _Worker(clock, granted).start(lambda w: w.claim(_P[0]))
    assert _until(lambda: granted == ["p1"])
    q.admit(_adm(_H1, _H2))

    def b_script(w: _Worker) -> None:
        assert w.clock.refusal(None, [_P[1]]) == "take"
        for adm in w.clock.take_holders():
            for site, _t in adm.held_tests:
                w.claim(FnLocator(site.file, site.fn_name))
    b = _Worker(clock, granted).start(b_script)
    assert _until(lambda: "h1" in granted)
    c = _Worker(clock, granted).start(lambda w: _taking(w, _P[2], hold=True))
    side = _until(lambda: "h2" in granted, timeout=1.0)
    for name in ("p1", "h1", "h2", "p3"):
        a.release(name)
        b.release(name)
        c.release(name)
    assert _until(lambda: len(granted) == 4)
    return granted, side


def test_holders_taken_together_run_side_by_side_within_the_share() -> None:
    """The line orders starts, not runs: while h1 is in flight, the next
    claim takes h2 and is granted, and the plan unit starts after both."""
    granted, side = _side_by_side(take_all=False)
    assert side, "h2 waited for h1 to return"
    assert granted[:3] == ["p1", "h1", "h2"]


def test_negative_control_both_holders_on_one_thread_run_one_after_another() -> None:
    _granted, side = _side_by_side(take_all=True)
    assert not side


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


# ---- the pool keeps its idle workers as claimers -----------------------------

class _Script:
    """A fill whose units block on their own release, and whose keeps
    measure holders: they only move the fake held view (the kept mark).
    Nothing is admitted here; the gate (:meth:`gate`) admits at whatever
    claim reads the view next, as `_GroundGate._claim` does."""

    def __init__(self, *, block: tuple[str, ...] = (),
                 keeps: dict[str, tuple[FnLocator, ...]] | None = None,
                 after: dict[str, tuple[str, ...]] | None = None,
                 last_keeps: tuple[FnLocator, ...] = ()) -> None:
        self.lock = threading.Lock()
        #: Measured by the keep of whichever plan unit returns last.
        self.last_keeps = last_keeps
        self.starts: list[tuple[str, str]] = []
        self.ends: list[str] = []
        self.go = {n: threading.Event() for n in block}
        self.keeps = keeps or {}
        self.after = after or {}
        self.done: dict[str, threading.Event] = {}
        self.measured: list[FnLocator] = []
        self.admitted: set[FnLocator] = set()

    def _done(self, name: str) -> threading.Event:
        with self.lock:
            return self.done.setdefault(name, threading.Event())

    def run(self, _idx: int, rec: Any, *, ctx: Any) -> FillResult:
        name = rec.fn_name
        with self.lock:
            self.starts.append((name, threading.current_thread().name))
        for first in self.after.get(name, ()):
            self._done(first).wait(5)
        if name in self.go:
            self.go[name].wait(5)
        with self.lock:
            self.measured.extend(self.keeps.get(name, ()))
            if (self.last_keeps and name in {u.fn_name for u in _P}
                    and len(set(self.ends) & {u.fn_name for u in _P}) == 3):
                self.measured.extend(self.last_keeps)
            self.ends.append(name)
        self._done(name).set()
        return FillResult(file=rec.file, fn_name=name, filled=True, attempts=1)

    def gate(self, q: HolderQueue) -> Callable[[Any, frozenset[FnLocator]], str]:
        def _g(_scope: Any, _units: frozenset[FnLocator]) -> str:
            with self.lock:
                new = [u for u in self.measured if u not in self.admitted]
                self.admitted.update(new)
            if new:
                q.admit(_adm(*new))
            return ""
        return _g

    def started(self) -> list[str]:
        with self.lock:
            return [n for n, _t in self.starts]

    def thread_of(self, name: str) -> str:
        with self.lock:
            return next(t for n, t in self.starts if n == name)

    def release_all(self) -> None:
        for ev in self.go.values():
            ev.set()


def _pass(tmp_path: Path, script: _Script, *, width: int = 4,
          claimer: bool = True, ask: bool = True,
          stop: Callable[[str], None] | None = None,
          ) -> tuple[threading.Thread, list[BaseException]]:
    """The real `_per_fn_passes` over p1..p4, on its own thread. A price
    that fits more than one call in the 13-minute share, so the share does
    not serialise the pool. ``claimer=False`` is the pool before this
    change; ``ask=False`` is the claimer state with no gate ask."""
    from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
    from pipeline_core.domain.enums import FillUnit
    from tests.test_fill_engine_usecases import FakeFill, _engine, _mk_ws

    ws = _mk_ws(tmp_path)
    recs = [{"file": u.file, "fn_name": u.fn_name, "container": "",
             "sig_text": f"pub fn {u.fn_name}()"} for u in (*_P, _H1, _H2)]
    fake = FakeFill(stub_records=recs, run=(0, ""), regr=None)
    if stop is not None:
        fake.check_stop = stop  # type: ignore[method-assign]
    base = _engine(fake, FillUnit.FN)
    eng = replace(base, fill_one=script,  # type: ignore[arg-type]
                  config=replace(base.config, parallelism=width))
    q = HolderQueue()
    clock = RunClock(share_minutes=13.0, fill_remaining=lambda: 100.0,
                     now=lambda: 0.0, yields_to=script.gate(q), holders=q,
                     call_price=1.0)
    errors: list[BaseException] = []

    def _go() -> None:
        try:
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
                claim_release=clock.release, take_holders=clock.take_holders,
                holders=q if claimer else None,
                admit_measured=clock.admit_measured if ask else None)
        except BaseException as e:  # noqa: BLE001 - the stop is the reading
            errors.append(e)
    th = threading.Thread(target=_go, daemon=True)
    th.start()
    return th, errors


def _together(tmp_path: Path, **kw: Any) -> tuple[_Script, bool, threading.Thread]:
    """p1 and p2 stay inside their units. p3 returns at once, so its worker
    has an empty plan queue while p1 and p2 are in flight. p4's keep, after
    p3 returned, measures two holders; h1 then stays inside its unit."""
    script = _Script(block=("p1", "p2", "h1"),
                     keeps={"p4": (_H1, _H2)}, after={"p4": ("p3",)})
    th, _errors = _pass(tmp_path, script, **kw)
    both = _until(lambda: {"h1", "h2"} <= set(script.started()), timeout=1.5)
    return script, both, th


def test_holders_admitted_together_start_on_idle_workers_while_plan_units_run(
        tmp_path: Path) -> None:
    script, both, th = _together(tmp_path)
    try:
        assert both, f"holders did not start while p1, p2 ran: {script.started()}"
        assert "p1" not in script.ends and "p2" not in script.ends
        order = script.started()
        assert order.index("h1") < order.index("h2")
        assert script.thread_of("h1") != script.thread_of("h2")
        # No plan unit started between the admission and the holders.
        assert order[-2:] == ["h1", "h2"]
    finally:
        script.release_all()
        th.join(5)
    assert not th.is_alive()


def test_negative_control_a_worker_that_returns_on_an_empty_queue_starts_no_holder(
        tmp_path: Path) -> None:
    script, both, th = _together(tmp_path, claimer=False)
    try:
        assert not both
        assert "h2" not in script.started()
    finally:
        script.release_all()
        th.join(5)
    assert not th.is_alive()


def test_the_pass_returns_after_the_holders_it_took_are_written(
        tmp_path: Path) -> None:
    script, both, th = _together(tmp_path)
    assert both
    script.release_all()
    th.join(5)
    assert not th.is_alive()
    assert {"h1", "h2"} <= set(script.ends)
    assert sorted(script.ends) == ["h1", "h2", "p1", "p2", "p3", "p4"]


def test_a_pass_with_no_admission_returns(tmp_path: Path) -> None:
    script = _Script()
    th, errors = _pass(tmp_path, script)
    th.join(5)
    assert not th.is_alive() and not errors
    assert sorted(script.ends) == ["p1", "p2", "p3", "p4"]


def _keep_while_parked(tmp_path: Path, **kw: Any) -> tuple[_Script, bool, threading.Thread]:
    """p1 stays inside its unit; p2 and p3 return at once (their workers
    park on an empty plan queue); p4's keep, after both returned, measures
    one holder and p4's worker finds the plan queue empty. Only the mark
    moves: no claim follows that keep."""
    script = _Script(block=("p1",), keeps={"p4": (_H1,)},
                     after={"p4": ("p2", "p3")})
    th, _errors = _pass(tmp_path, script, **kw)
    started = _until(lambda: "h1" in script.started(), timeout=1.5)
    return script, started, th


def test_a_keep_whose_worker_claims_nothing_after_it_is_admitted_by_an_idle_worker(
        tmp_path: Path) -> None:
    script, started, th = _keep_while_parked(tmp_path)
    try:
        assert started, f"h1 waited for p1: {script.started()}"
        assert "p1" not in script.ends
    finally:
        script.release_all()
        th.join(5)
    assert not th.is_alive()


def test_negative_control_with_no_gate_ask_the_measured_holder_waits(
        tmp_path: Path) -> None:
    """The plan before its review: idle workers park, but nobody makes the
    claim the gate admits at."""
    script, started, th = _keep_while_parked(tmp_path, ask=False)
    try:
        assert not started
    finally:
        script.release_all()
        th.join(5)
    assert not th.is_alive()


@pytest.mark.parametrize("width", [4, 1])
def test_a_holder_the_last_unit_in_flight_measures_is_written_before_the_join(
        tmp_path: Path, width: int) -> None:
    """Every other worker is parked when p1's keep measures h1; the level
    must not close before h1 is admitted and written."""
    script = (_Script(block=("p1",), keeps={"p1": (_H1,)},
                      after={"p1": ("p2", "p3", "p4")}) if width > 1
              else _Script(last_keeps=(_H1,)))
    th, errors = _pass(tmp_path, script, width=width)
    if width > 1:
        assert _until(lambda: {"p2", "p3", "p4"} <= set(script.ends))
        _pause(0.1)            # the three idle workers are parked
    script.release_all()
    th.join(5)
    assert not th.is_alive() and not errors
    assert script.ends[-1] == "h1"


def test_negative_control_without_the_ask_the_last_keep_is_lost_to_the_level(
        tmp_path: Path) -> None:
    script = _Script(block=("p1",), keeps={"p1": (_H1,)},
                     after={"p1": ("p2", "p3", "p4")})
    th, _errors = _pass(tmp_path, script, ask=False)
    assert _until(lambda: {"p2", "p3", "p4"} <= set(script.ends))
    script.release_all()
    th.join(5)
    assert not th.is_alive()
    assert "h1" not in script.started()


# A stop raises SystemExit in each worker thread, as in the live pool; the
# main thread's re-raise after the join is the reading.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_stop_while_workers_are_parked_ends_the_pass(tmp_path: Path) -> None:
    stopping = threading.Event()

    def _stop(_context: str) -> None:
        if stopping.is_set():
            raise SystemExit("graceful stop")
    script = _Script(block=("p1",))
    th, errors = _pass(tmp_path, script, stop=_stop)
    assert _until(lambda: {"p2", "p3", "p4"} <= set(script.ends))
    _pause(0.1)                # p2..p4's workers are parked behind p1
    stopping.set()
    _pause(0.1)
    script.release_all()       # the unit in flight completes
    th.join(5)
    assert not th.is_alive()
    assert errors and isinstance(errors[0], SystemExit)
