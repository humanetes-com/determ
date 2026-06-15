"""Unit tests for the shared fix-loop driver (pipeline_core.application.fix_loop):
accept / dead-end-stop / floor-stall / repeated-signature / hard-cap exits, the
initial_floor seeding, the callbacks, and the check_stop hook. The driver is pure
(domain FloorStall gauge + injected `one_attempt`), so fake callables drive it."""
from __future__ import annotations

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop


def _runner(outcomes: list[FixOutcome]):
    """one_attempt that returns each scripted outcome in turn (records attempt #s)."""
    seen: list[int] = []

    def one_attempt(n: int) -> FixOutcome:
        seen.append(n)
        return outcomes[n - 1]

    return one_attempt, seen


def test_accepted_first_attempt() -> None:
    one, seen = _runner([FixOutcome("accepted")])
    assert drive_fix_loop(one, hard_cap=5, max_stalls=3, stop_context="x") == (True, 1)
    assert seen == [1]


def test_stop_is_immediate_dead_end() -> None:
    one, _ = _runner([FixOutcome("stop"), FixOutcome("accepted")])
    assert drive_fix_loop(one, hard_cap=5, max_stalls=3, stop_context="x") == (False, 1)


def test_hard_cap_when_improving_never_stalls() -> None:
    # Strictly decreasing err_count → floor improves every attempt → no stall;
    # distinct signatures → repeated never fires → runs to the cap.
    outs = [FixOutcome("failed", err_sig=frozenset({str(c)}), err_count=c)
            for c in (5, 4, 3)]
    one, seen = _runner(outs)
    assert drive_fix_loop(one, hard_cap=3, max_stalls=9, stop_context="x") == (False, 3)
    assert seen == [1, 2, 3]


def test_floor_stall_exit_and_callbacks() -> None:
    # Flat err_count (no improvement), no signature → stall counter climbs.
    outs = [FixOutcome("failed", err_count=5) for _ in range(5)]
    one, _ = _runner(outs)
    failures: list[int] = []
    stalls: list[int] = []
    res = drive_fix_loop(
        one, hard_cap=5, max_stalls=2, stop_context="x",
        on_failure=lambda n, o, g: failures.append(n),
        on_stall=lambda n, g: stalls.append(n))
    # attempt1 sets the floor (stalls=0); attempts 2,3 stall → exit at 3.
    assert res == (False, 3)
    assert failures == [1, 2, 3] and stalls == [3]


def test_repeated_signature_exit() -> None:
    # Improving count (no stall) but identical non-None signatures two in a row.
    sig = frozenset({"src/a.rs:E0277"})
    outs = [FixOutcome("failed", err_sig=sig, err_count=5),
            FixOutcome("failed", err_sig=sig, err_count=4)]
    one, _ = _runner(outs)
    repeated: list[int] = []
    res = drive_fix_loop(one, hard_cap=9, max_stalls=9, stop_context="x",
                         on_repeated=lambda n, g: repeated.append(n))
    assert res == (False, 2) and repeated == [2]


def test_initial_floor_seeds_stall() -> None:
    # Seeded floor=3; a first failure that fails to beat 3 counts as a stall.
    one, _ = _runner([FixOutcome("failed", err_count=3)])
    res = drive_fix_loop(one, hard_cap=5, max_stalls=1, stop_context="x",
                         initial_floor=3)
    assert res == (False, 1)


def test_check_stop_hook_invoked() -> None:
    # #14: check_stop is now an explicit per-call parameter (was a module global).
    contexts: list[str] = []
    one, _ = _runner([FixOutcome("accepted")])
    drive_fix_loop(one, hard_cap=2, max_stalls=2, stop_context="ctxlabel",
                   check_stop=contexts.append)
    assert contexts == ["ctxlabel attempt 1"]
