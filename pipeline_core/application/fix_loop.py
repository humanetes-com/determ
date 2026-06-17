"""THE single fix-loop driver (P3 unification, FINAL-RESULT PLAN).

One loop engine — :func:`run_fix_loop` — owns the shape every fix loop used to
hand-roll: iterate up to a hard cap, run one attempt, and exit on
accept / dead-end / stall-budget / repeated-signature. Two *views* ride it,
because the pipeline genuinely needs both and the union loses neither:

* the **outcome view** (:func:`drive_fix_loop`): an attempt reports a
  :class:`FixOutcome` (``accepted`` / ``stop`` / ``failed`` + an error
  signature). Built for inner compile loops where the signal is structured —
  a ``stop`` is a protocol dead-end (retrying can't help) and two identical
  error signatures in a row is churn worth bailing on. The gauge is the
  lower-is-better :class:`FloorStall`.

* the **metric view** (:func:`pipeline_core.application.refine_loop.run_refine_loop`):
  an attempt reports a higher-is-better metric (coverage %, pass count,
  −error count) and the loop layers retrospective-on-stall + the
  :class:`LoopState` meta-awareness narrative. Built for outer "keep improving
  a number" loops. The gauge is :class:`LoopState` itself.

Both views are thin facades; the iterate/stall/repeated/budget policy lives
once, in :func:`run_fix_loop`. The gauge is injected (a :class:`StallGauge` —
both ``FloorStall`` and ``LoopState`` satisfy it); the engine only *reads*
``stalls`` / ``repeated`` and owns the exit arithmetic, while each view's
``attempt`` closure owns the mutation (``FloorStall.observe`` /
``LoopState.record``) and the I/O.

`check_stop` is an explicit per-call hook (#14: was a module-global wired via
`configure()`): each caller passes its graceful-stop check. The default no-op
keeps the loop import-safe and tests hermetic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pipeline_core.domain.refine import FloorStall


def _no_stop(_context: str) -> None:
    """Default graceful-stop hook: never interrupts."""


def _noop(_attempt: int) -> None:
    """Default per-attempt hook: does nothing."""


class StallGauge(Protocol):
    """The minimal gauge :func:`run_fix_loop` reads to make its exit decisions.

    Both :class:`FloorStall` (lower-is-better count gauge) and
    :class:`LoopState` (higher-is-better metric history) satisfy it. The engine
    never mutates the gauge — the view's ``attempt`` closure does (observe /
    record) — it only reads these two fields after each attempt."""

    @property
    def stalls(self) -> int:
        """Consecutive non-improving attempts so far."""
        ...

    @property
    def repeated(self) -> bool:
        """True when the last two attempts carried the same non-None signature."""
        ...


class StepStatus(StrEnum):
    """What one attempt tells the engine to do next.

    CONTINUE  — a failed/measured attempt; the engine consults the gauge for
                stall / repeated exits, else loops.
    ACCEPTED  — success (compiles / tests pass / metric target reached); the
                metric view has already recorded this attempt before returning.
    DEAD_END  — protocol dead-end (empty response, splice rejected, race lost):
                exit now, retrying cannot help.
    """

    CONTINUE = "continue"
    ACCEPTED = "accepted"
    DEAD_END = "dead_end"


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why :func:`run_fix_loop` stopped, and after how many attempts."""

    attempts: int
    reason: str  # "accepted" | "dead_end" | "stalled" | "repeated" | "hard_cap" | "done"


def run_fix_loop(
    attempt: Callable[[int], StepStatus],
    gauge: StallGauge,
    *,
    hard_cap: int,
    max_stalls: int,
    before_attempt: Callable[[int], None] = _noop,
    initial_done: bool = False,
) -> LoopOutcome:
    """Run ``attempt`` until it accepts / dead-ends / the gauge stalls out /
    the gauge reports a repeated signature / the hard-cap backstop fires.

    `initial_done` short-circuits with zero attempts (the metric view's
    "already at target" case). `before_attempt(n)` runs once per attempt before
    it (graceful-stop check for the outcome view; pending-retro setup for the
    metric view). The stall / repeated checks read the gauge AFTER the attempt —
    in that order, matching the legacy per-fn/per-impl loops — so the attempt's
    own mutation (observe / record) is visible to the exit decision."""
    if initial_done:
        return LoopOutcome(0, "done")
    n = 0
    while n < hard_cap:
        n += 1
        before_attempt(n)
        status = attempt(n)
        if status is StepStatus.DEAD_END:
            return LoopOutcome(n, "dead_end")
        if status is StepStatus.ACCEPTED:
            return LoopOutcome(n, "accepted")
        if gauge.stalls >= max_stalls:
            return LoopOutcome(n, "stalled")
        if gauge.repeated:
            return LoopOutcome(n, "repeated")
    return LoopOutcome(n, "hard_cap")


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """One fix-loop attempt's result, returned by a unit's attempt callable.

    kind: "accepted" — the unit accepted and finalized the fill;
          "stop"     — protocol dead-end (empty response, splice rejected,
                       race lost, LLM error): retrying cannot help;
          "failed"   — compile/test failure: the driver's gauge decides
                       whether to retry (err_sig/err_count feed FloorStall).
    """
    kind: str
    # None = "this unit has no error signature" → the driver's repeated-
    # signature exit never fires (loop C / f4: failing-test counts carry no
    # rustc sig). An EMPTY frozenset is a real signature: two consecutive
    # empty sigs (rc!=0, zero parsed diagnostics) trigger the repeated exit,
    # as the hand-rolled per-fn/per-impl loops always did.
    err_sig: frozenset[str] | None = None
    err_count: int = 0


def drive_fix_loop(
    one_attempt: Callable[[int], FixOutcome],
    *,
    hard_cap: int,
    max_stalls: int,
    stop_context: str,
    on_failure: Callable[[int, FixOutcome, FloorStall], None] | None = None,
    on_stall: Callable[[int, FloorStall], None] | None = None,
    on_repeated: Callable[[int, FloorStall], None] | None = None,
    initial_floor: int | None = None,
    check_stop: Callable[[str], None] = _no_stop,
) -> tuple[bool, int]:
    """Outcome-view facade over :func:`run_fix_loop`. Run `one_attempt` until
    accepted / dead-end / stall / repeated sig / hard cap; returns
    (accepted, attempts_used). The :class:`FloorStall` gauge is seeded with
    `initial_floor` (the metric the loop ENTERED with — a first attempt that
    fails to beat it counts as a stall, not a free baseline). `check_stop` is
    invoked once per attempt with the stop context (graceful-shutdown hook)."""
    gauge = FloorStall()
    if initial_floor is not None:
        gauge.observe(initial_floor)
    accepted = False

    def attempt(n: int) -> StepStatus:
        nonlocal accepted
        outcome = one_attempt(n)
        if outcome.kind == "accepted":
            accepted = True
            return StepStatus.ACCEPTED
        if outcome.kind == "stop":
            return StepStatus.DEAD_END
        gauge.observe(outcome.err_count, signature=outcome.err_sig)
        if on_failure is not None:
            on_failure(n, outcome, gauge)
        return StepStatus.CONTINUE

    result = run_fix_loop(
        attempt, gauge, hard_cap=hard_cap, max_stalls=max_stalls,
        before_attempt=lambda n: check_stop(f"{stop_context} attempt {n}"),
    )
    if result.reason == "stalled" and on_stall is not None:
        on_stall(result.attempts, gauge)
    if result.reason == "repeated" and on_repeated is not None:
        on_repeated(result.attempts, gauge)
    return accepted, result.attempts
