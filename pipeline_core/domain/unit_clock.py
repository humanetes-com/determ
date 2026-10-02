"""Wall-clock bound on ONE claimed unit of a fill route, per thread.

WHY. The fill schedule's share is checked between units, never inside one: a
unit in flight always completes. Nothing bounded how long one unit could take,
so a single unit could spend the whole fill. Run `484518-91681c1a` (iter 4,
2026-09-23): `impl src/error.rs` opened on a 15-minute share and closed
`REVERTED after 199.0 min (11 call(s), net +0, kept 0, reverted 9)` — five fix
attempts, a floor stall, the maximal last-resort call, a retrospective, five
more attempts, a second retrospective — and never compiled; the fill's
200-minute budget was gone and every other run of that fill was deferred.
`scripts/share_overrun.py` over the week before (211 route runs) put the p90
minutes/share of every route kind between 1.80 and 3.40 and the impl route's
max at 12.94, that run.

WHAT. The run's clock (`fill_schedule.RunClock`) starts this clock on the
claiming thread at every successful claim, with a bound of
``FILL_UNIT_SHARE_MULTIPLE`` × the run's share. The fix loops read
:func:`expired` at their attempt boundaries — the shared driver
(`fix_loop.run_fix_loop`, via `drive_fix_loop`) before every attempt after
the first, and the per-fn / per-impl retrospective loops before a
last-resort call or a retrospective — so a unit ends at the next boundary
past its bound with the exit its route already has for a failed unit. The
attempt in flight completes. A thread that never claimed (task0c's loops, a
test) has no bound, so a loop that reads the clock outside a fill is
unaffected.

The same boundary carries the claim's GROUND. A unit is claimed on a plan;
a keep elsewhere in the fill can move every test that plan aimed it at while
the unit is still retrying. The run's clock asked the fill's premise gate
when the unit was claimed and never again, so a later call of the same unit
was bought on ground that had already moved: run `3040-51eab48c`
(2026-10-02, fill 5) kept a file body at 08:16:09 that turned the last red
test green, and two file runs of the same wave then each opened a second
attempt for a peeled name with no red test left to judge the reply. The
claim hands this clock its own ground question (``ground``), and
:func:`ended` answers both at once: ``"premise"`` when the ground the claim
was planned on has moved, ``"bound"`` when the unit is past its bound, ``""``
otherwise. Every loop that buys a later call for a claimed unit reads
:func:`ended` — the shared fix-loop driver, the retrospective loops, and the
file and pair attempt loops, which have no fix loop of their own.

This is not a stop: the process, the fill and the other runs continue. The
process-level bound stays `fill_phase_clock`.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

_local = threading.local()


def start(bound_minutes: float | None, *,
          now: Callable[[], float] = time.monotonic,
          ground: Callable[[Iterable[str] | None], bool] | None = None,
          ) -> None:
    """Begin a unit on this thread. ``None`` or ``<= 0`` means unbounded.
    ``ground(tests)`` answers True once the ground has moved under
    ``tests`` — or, with ``None``, under the scope the unit was claimed
    on; ``ground=None`` means the claim carried no premise gate."""
    _local.now = now
    _local.bound = bound_minutes if bound_minutes and bound_minutes > 0 else None
    _local.started = now()
    _local.noted = False
    _local.ground = ground


def clear() -> None:
    """No unit on this thread (tests, and the end of a fill)."""
    _local.bound = None
    _local.noted = False
    _local.ground = None


def bound_minutes() -> float | None:
    """This thread's current unit bound in minutes, or None."""
    return getattr(_local, "bound", None)


def elapsed_minutes() -> float:
    """Minutes since this thread's unit started; 0 when none started."""
    now = getattr(_local, "now", None)
    if now is None:
        return 0.0
    started: float = _local.started
    return (float(now()) - started) / 60.0


def expired() -> bool:
    """True when this thread's unit has run past its bound."""
    bound = bound_minutes()
    return bound is not None and elapsed_minutes() >= bound


def ended(tests: Iterable[str] | None = None) -> str:
    """Why this thread's unit may not buy another call: ``"premise"`` when
    the ground under the call has moved, ``"bound"`` when the unit has run
    past its bound, ``""`` when it may. The premise is asked first, as the
    claim itself asks it (`fill_schedule.RunClock.refusal`). A thread that
    never claimed answers ``""``.

    ``tests`` is the tests of the NAMES the next call will ask — a retry
    of two peeled names asks on those two names' tests, not on the claim's
    whole scope. Asked on the claim's scope, a peel whose own tests had all
    moved was still bought while any other planned test of its file stayed
    red: the gate stands a claim on the part of its ground that has not
    moved. An empty set is a call that asks no named test and is refused
    once anything in the fill has moved. ``None`` asks on the claim's own
    scope — a unit whose every call asks the same names."""
    ground = getattr(_local, "ground", None)
    if ground is not None and ground(tests):
        return "premise"
    return "bound" if expired() else ""


def note_once() -> bool:
    """True the first time an ended unit is noticed, so the caller logs
    and emits once per unit however many boundaries read it. Call after
    :func:`ended` answered non-empty."""
    if getattr(_local, "noted", False):
        return False
    _local.noted = True
    return True


def end_note(reason: str, *, attempts: int) -> tuple[str, str, dict[str, object]]:
    """The log clause, event name and event payload for a unit that ended
    on ``reason`` (:func:`ended`), so every route says it the same way."""
    if reason == "premise":
        return (f"PREMISE_MOVED — the tests this unit was claimed on no "
                f"longer fail where the plan read them, after {attempts} "
                "attempt(s); no further call, left for the sweep's re-plan",
                "phase_c.unit.premise_moved", {"attempts": attempts})
    return (f"UNIT_BOUND — {elapsed_minutes():.0f} min past its "
            f"{bound_minutes() or 0:.0f}-min bound after {attempts} "
            "attempt(s)",
            "phase_c.unit.bound",
            {"attempts": attempts, "bound_minutes": bound_minutes(),
             "elapsed_minutes": round(elapsed_minutes(), 1)})
