"""Progress INSIDE the `fill` step of one TDD iteration.

WHY THIS EXISTS. The iteration strip
(:mod:`pipeline_core.monitor.iteration_steps`) named the five steps and made
one thing visible immediately: the loop is in `fill` for almost the whole
iteration. Run `1383168-eb151d09` (2026-08-29) iter 1 measured it — the sweep
took 0.7 s, grade/aim/frontier finished inside two seconds, and the fill ran
21:32:27 -> 23:15:04 UTC. So `▶fill` for 1 h 43 m was the panel's entire answer
to "what is it doing", and one level further down is where the readings are.

WHAT A FILL IS MADE OF. `task2_fill_fns_one_by_one` walks three ROUTES in
order, each of which spends model calls on UNITS of a different size, and each
unit runs a fix loop of ATTEMPTS. The same iteration, as the events recorded
it:

    route     unit                              spent      what one call writes
    file      src/error.rs (2 stub, 2 re-fill)  8m42s      every body in the file
    impl      impl Error (2 stub, 1 re-fill)    48m35s     every body in the block
    per-fn    object_drop, construct_from_boxed,
              backtrace, context (4, parallel)  45m17s     one body

Bodies are what lands in all three; the routes differ in how many the prompt
designs together, and a unit that fails one route falls through to the next —
`context` and `construct_from_boxed` were attempted by all three that
iteration. That fall-through is why this module reports the routes SEPARATELY
and never sums them into one bar: the sum double-counts exactly the units the
iteration is having the most trouble with.

THE DENOMINATORS ARE READ, NEVER GUESSED. Each route publishes the unit count
it is about to walk — the file and impl routes as `phase_c.fill.route_plan`
(`application/reconstruct/impl_fill.py`), the per-fn route as
`phase_c.pass.start.stubs` — so `2/5` on the strip is that route's own plan,
not an inference from the frontier or from the dispatch cap. Records written
before the route_plan emit existed carry no plan, and a route with no plan
shows its started/finished counts with no bar rather than borrowing a
denominator from somewhere else.

INSIDE A UNIT, THE ATTEMPT COUNT IS A COUNTER AND NOT A FRACTION. The file
route is capped (`PHASE_C_FILE_MAX_ATTEMPTS`, and `file.fill.revert` carries
`attempt`/`max_attempts`), so it renders as `attempt 2/3`. The impl and per-fn
fix loops are floor-STALL bounded — they stop when the best error count has not
improved for `PHASE_C_IMPL_MAX_STALLS` attempts, and that bound is adjusted per
module before use — so there is no attempt ceiling in the stream to divide by.
What those two publish per attempt (`loop.iter`) is the pair that actually
answers "is this unit converging": the floor (best metric so far) and the
stall count. A falling floor is the model closing on the body; a climbing stall
is it re-buying the same wrong answer, which is the shape worth interrupting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ROUTES", "FillRoute", "FillUnit", "FillProgress", "fold_fill"]

# Display order for routes NOT yet observed this iteration. Observed routes are
# reported in the order the events put them in, so a `PHASE_C_ROUTE_ORDER`
# change (or a route the iteration skips) can never draw a ✓ for a route that
# has not run.
ROUTES: tuple[str, ...] = ("file", "impl", "per-fn")

# `phase_c.<x>.fill.*` -> route id. The per-fn route's own events are
# `phase_c.fn.fill.*`; its plan arrives on `phase_c.pass.start`.
_EVENT_ROUTE: dict[str, str] = {"file": "file", "impl": "impl", "fn": "per-fn"}

# Terminal for a unit's CURRENT attempt. A later `start` for the same key
# re-opens it (the file route retries a reverted file up to its cap), which is
# why `started` counts distinct keys and never `start` events.
_TERMINAL = ("done", "fail", "skip", "revert", "mismatch", "resample_skipped",
             "already_complete")


@dataclass(frozen=True, slots=True)
class FillRoute:
    """One route's position: units it opened, units it closed, units it
    planned. `planned` is None when the records carry no plan for it."""

    id: str
    started: int
    finished: int
    planned: int | None

    @property
    def denominator(self) -> int | None:
        """What a bar may be drawn against, or None for no bar.

        The plan when there is one — and `started` can exceed it (a route may
        open a unit the plan did not count), so the larger of the two wins
        rather than a bar pinned past full.
        """
        if self.planned is None:
            return None
        return max(self.planned, self.started)


@dataclass
class _Unit:
    route: str
    label: str
    file: str
    members: int
    refill: bool
    thread: str
    since_iso: str
    attempt: int = 0
    max_attempts: int | None = None
    floor: str = ""
    stalled: int = 0
    improving: bool | None = None
    live: bool = True


@dataclass(frozen=True, slots=True)
class FillUnit:
    """One unit of one route, with its fix-loop reading.

    `attempt` is the number of model attempts OBSERVED to have failed on this
    unit, so 0 means the first answer has not come back yet — never a guess at
    which attempt is in flight. `floor`/`stalled` are the fix loop's own
    published gauge (`loop.iter`); both are empty until the first attempt fails,
    because until then the loop has measured nothing.
    """

    route: str
    label: str
    file: str
    members: int
    refill: bool
    thread: str
    since_iso: str
    attempt: int
    max_attempts: int | None
    floor: str
    stalled: int
    improving: bool | None


@dataclass(frozen=True, slots=True)
class FillProgress:
    """The fill step as the events since the last `phase_c.tdd.iter` describe
    it. `current` is the route of the newest fill event — observed, never
    inferred from where the other routes are."""

    routes: tuple[FillRoute, ...] = ()
    current: str | None = None
    batch: int | None = None
    live: tuple[FillUnit, ...] = field(default=())

    def __bool__(self) -> bool:
        return bool(self.routes)


def _route_of(name: str) -> tuple[str, str] | None:
    """(route id, terminal-or-'start' suffix) for a `phase_c.*.fill.*` name."""
    parts = name.split(".")
    if len(parts) < 4 or parts[0] != "phase_c" or parts[2] != "fill":
        return None
    route = _EVENT_ROUTE.get(parts[1])
    return (route, ".".join(parts[3:])) if route else None


def fold_fill(events: list[dict[str, Any]]) -> FillProgress:
    """Fold the event stream into the CURRENT fill's picture.

    Every `phase_c.tdd.iter` clears the accumulator: that event is emitted at
    the end of the frontier step, immediately before the fill call, so the
    records after the last one are this fill and nothing else. An iteration
    that dispatched nothing emits it too and simply accumulates no routes,
    which is the empty `FillProgress` the caller renders as no rows.
    """
    order: list[str] = []
    plans: dict[str, int] = {}
    units: dict[str, _Unit] = {}
    current: str | None = None
    batch: int | None = None

    def _open(route: str, key: str, unit: _Unit) -> None:
        nonlocal current
        if route not in order:
            order.append(route)
        current = route
        prev = units.get(key)
        if prev is None:
            units[key] = unit
        else:
            # A retry of a unit already opened: keep its accumulated gauge and
            # re-open it, so the file route's three attempts stay ONE unit.
            prev.live = True
            prev.since_iso = unit.since_iso
            prev.thread = unit.thread

    for e in events:
        name = e.get("event", "")
        d = e.get("data") or {}
        if name == "phase_c.tdd.iter":
            order, plans, units, current = [], {}, {}, None
            _b = d.get("dispatched")
            batch = _b if isinstance(_b, int) else None
            continue
        if name == "phase_c.fill.route_plan":
            _r, _u = d.get("route"), d.get("units")
            if _r in ROUTES and isinstance(_u, int):
                plans[_r] = _u
            continue
        if name == "phase_c.pass.start":
            _s = d.get("stubs")
            if isinstance(_s, int):
                plans["per-fn"] = _s
            continue
        if name == "loop.iter":
            _apply_loop_iter(units, d)
            continue
        hit = _route_of(name)
        if hit is None:
            continue
        route, suffix = hit
        key = _unit_key(route, d)
        if suffix == "start":
            _open(route, key, _Unit(
                route=route, label=_label(route, d), file=str(d.get("file") or ""),
                members=(int(d.get("stubs", 0) or 0) + int(d.get("refills", 0) or 0)
                         + int(d.get("stub_count", 0) or 0)
                         + int(d.get("refill_count", 0) or 0)) or 1,
                refill=bool(d.get("refills") or d.get("refill_count")
                            or d.get("refill")),
                thread=str(e.get("thread") or ""), since_iso=str(e.get("ts") or "")))
        elif suffix in _TERMINAL:
            u = units.get(key)
            if u is None:
                continue
            current = route
            # The file route's revert says whether it is coming back; a retry
            # leaves the unit open so the strip does not report it closed and
            # then re-open it one event later.
            if suffix == "revert":
                _a, _m = d.get("attempt"), d.get("max_attempts")
                if isinstance(_a, int):
                    u.attempt = _a
                if isinstance(_m, int):
                    u.max_attempts = _m
                if d.get("retrying"):
                    continue
            u.live = False

    routes = tuple(FillRoute(
        id=r,
        started=sum(1 for u in units.values() if u.route == r),
        finished=sum(1 for u in units.values() if u.route == r and not u.live),
        planned=plans.get(r),
    ) for r in order + [r for r in ROUTES if r not in order and r in plans])
    live = tuple(FillUnit(
        route=u.route, label=u.label, file=u.file, members=u.members,
        refill=u.refill, thread=u.thread, since_iso=u.since_iso,
        attempt=u.attempt, max_attempts=u.max_attempts, floor=u.floor,
        stalled=u.stalled, improving=u.improving)
        for u in units.values() if u.live)
    return FillProgress(routes=routes, current=current, batch=batch, live=live)


def _unit_key(route: str, d: dict[str, Any]) -> str:
    """A unit's identity across its own retries. The file route keys on the
    file (its `group` label repeats per attempt), the impl route on the
    container, the per-fn route on the fn."""
    file = str(d.get("file") or "?")
    if route == "file":
        return f"file:{file}"
    if route == "impl":
        return f"impl:{file}::{d.get('container') or '?'}"
    return f"fn:{file}::{d.get('fn') or '?'}"


def _label(route: str, d: dict[str, Any]) -> str:
    if route == "file":
        return str(d.get("file") or "?")
    if route == "impl":
        return str(d.get("container") or "?").splitlines()[0]
    return str(d.get("fn") or "?")


# `loop.iter` names, as the two fix loops emit them
# (`fill_one_fn.py::_on_fn_failure`, `impl_fill.py::_on_impl_failure`). Any
# other producer of this event name — the module-reconstruct improvement loop
# writes `impl:<file>::<container>` — belongs to a different loop entirely and
# is ignored rather than folded into a unit it does not describe.
_LOOP_ROUTE: dict[str, str] = {"phase_c.per_fn": "per-fn", "phase_c.impl": "impl"}


def _apply_loop_iter(units: dict[str, _Unit], d: dict[str, Any]) -> None:
    route = _LOOP_ROUTE.get(str(d.get("name") or ""))
    if route is None:
        return
    u = units.get(_unit_key(route, d))
    if u is None:
        return
    _it = d.get("iter")
    if isinstance(_it, int):
        u.attempt = _it
    u.floor = str(d.get("metric_best") or u.floor)
    _st = d.get("stalled")
    u.stalled = _st if isinstance(_st, int) else u.stalled
    u.improving = bool(d.get("improved"))
