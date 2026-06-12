"""THE fix-loop driver (slice 8a f2; relocated from pipeline.py in P1 of the
FINAL-RESULT PLAN so application-layer tasks can drive it too).

Owns the loop skeleton every fix loop used to hand-roll — attempt counting,
graceful-stop checks, FloorStall observation, and the stall / repeated-
signature terminations. The unit owns everything domain-specific inside
`one_attempt` (context gathering, prompt, splice + locking, verification,
acceptance, revert); callbacks carry the unit's telemetry (loop.iter emit,
stop logs).

Consumers: phase-c per-fn loop (f2), per-impl loop (f3), post-pass test
retry (f4), task0c harness-fix + gen_tests compile-fix loops (P1 tail).

`check_stop` is a settable hook (log.py facade pattern): the composition
root wires pipeline.py's graceful-stop sentinel check; the default no-op
keeps the module import-safe and tests hermetic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline_core.domain.refine import FloorStall

_check_stop: Callable[[str], None] = lambda _context: None


def configure(*, check_stop: Callable[[str], None]) -> None:
    """Wire the graceful-stop hook. Called once at the composition root."""
    global _check_stop
    _check_stop = check_stop


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
) -> tuple[bool, int]:
    """Run `one_attempt` until accepted / dead-end / stall / repeated sig /
    hard cap. Returns (accepted, attempts_used)."""
    gauge = FloorStall()
    if initial_floor is not None:
        # Seed the floor with the metric the loop ENTERED with (loop C / f4):
        # a first iteration that fails to improve on the entry state counts
        # as a stall, instead of getting a free baseline observation.
        gauge.observe(initial_floor)
    attempt_n = 0
    while attempt_n < hard_cap:
        attempt_n += 1
        _check_stop(f"{stop_context} attempt {attempt_n}")
        outcome = one_attempt(attempt_n)
        if outcome.kind == "accepted":
            return True, attempt_n
        if outcome.kind == "stop":
            return False, attempt_n
        gauge.observe(outcome.err_count, signature=outcome.err_sig)
        if on_failure is not None:
            on_failure(attempt_n, outcome, gauge)
        if gauge.stalled(max_stalls):
            if on_stall is not None:
                on_stall(attempt_n, gauge)
            return False, attempt_n
        if gauge.repeated:
            if on_repeated is not None:
                on_repeated(attempt_n, gauge)
            return False, attempt_n
    return False, attempt_n
