"""A stub a failing test panics in while it unwinds is a holder the fill
takes, and it is taken first.

Run `630503-f5ca6540` (2026-11-09, cluster `stable`): after the keep of the
error constructor, every failing test's process aborted on the runtime's
double panic in `Drop for Error`, a stub. The hold read only the first
failure (`StubHold.by_test`), which named the destructor for 2 tests, and
the in-fill holder admission listed it 10th of 18 by file sort. It was kept
about three hours later, while every reading re-ran the aborted binaries one
test per process.

Invented shape only: a test first fails in a stub at `src/a.rs:3`, then
unwinds into a stub at `src/z.rs:12` (a later file, so the file sort puts it
second), and the runtime announces the panic in cleanup."""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_scheduler import (
    _holder_run,
    unwritten_holders,
)
from pipeline_core.domain.fill_asks import FillAsks
from pipeline_core.domain.held_stubs import HeldStubs
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_outcome import holder_order, stub_hold_of
from pipeline_core.domain.test_results import ABORT_MARK, STDERR_MARK

A = FnSite("src/a.rs", "fmt", "impl Display for Error")
B = FnSite("src/z.rs", "drop", "impl Drop for Error")
_SITES = {("src/a.rs", 3): A, ("src/z.rs", 12): B}

_RUNTIME = ("thread 'b' panicked at /rustc/0000/library/core/src/panicking.rs:233:5:\n"
            "panic in a destructor during cleanup\n"
            "thread caused non-unwinding panic. aborting.")


def _block(*panics: tuple[str, int]) -> str:
    return "\n".join(f"thread 'b' panicked at {f}:{ln}:9:\nnot implemented"
                     for f, ln in panics)


def _capture(block_b: str) -> str:
    return ("running 2 tests\n"
            "test a ... ok\n"
            "test b ... FAILED\n\n"
            "failures:\n\n"
            "---- b stdout ----\n"
            f"{ABORT_MARK}\n{block_b}\n\n"
            "failures:\n    b\n\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
            f"\n{STDERR_MARK}\n"
            "     Running tests/holder.rs (target/debug/deps/holder-aaaa)\n")


def _locate(file: str, line: int) -> FnSite | None:
    return _SITES.get((file, line))


def _hold(block_b: str):
    return stub_hold_of(_capture(block_b), _locate, is_stub=lambda _s: True)


def test_the_stub_a_test_unwinds_through_is_its_unwinding_hold() -> None:
    hold = _hold(_block(("src/a.rs", 3), ("src/z.rs", 12)) + "\n" + _RUNTIME)
    assert hold.by_test == {"b": frozenset({A})}
    assert hold.unwinding == {"b": frozenset({B})}
    # Sight, the deferral and the session planner keep the first failure.
    assert HeldStubs.of(hold).by_test == {"b": frozenset({FnLocator(A.file, A.fn_name)})}


def test_no_double_panic_no_unwinding_hold() -> None:
    # The same two panics without the runtime's cleanup announcement: the
    # process did not abort on a panic in cleanup, so nothing unwound into B.
    hold = _hold(_block(("src/a.rs", 3), ("src/z.rs", 12)))
    assert hold.unwinding == {}


def test_a_first_failure_inside_the_destructor_is_not_counted_twice() -> None:
    hold = _hold(_block(("src/z.rs", 12)) + "\n" + _RUNTIME)
    assert hold.by_test == {"b": frozenset({B})}
    assert hold.unwinding == {}


def _admit(unwinding: dict[str, frozenset[FnSite]] | None) -> list[FnLocator]:
    return unwritten_holders(
        {"b": frozenset({A})}, ahead=(), planned_held=(), landed=(),
        opened=(), asked=FillAsks(), unwinding=unwinding)


def test_the_gate_admits_the_unwinding_stub_ahead_of_the_first_failure() -> None:
    a, b = FnLocator(A.file, A.fn_name), FnLocator(B.file, B.fn_name)
    assert _admit({"b": frozenset({B})}) == [b, a]
    # NEGATIVE CONTROL: the admission as it read before (first failure only)
    # never names the unwinding stub.
    assert _admit(None) == [a]


def test_the_holder_run_lists_the_unwinding_stub_first() -> None:
    a, b = FnLocator(A.file, A.fn_name), FnLocator(B.file, B.fn_name)
    held = {"b": frozenset({A}), "c": frozenset({A})}
    unwinding = {"b": frozenset({B})}
    run = _holder_run([b, a], held, parent="per-fn", seq=1000,
                      bodies_sha=lambda _t: "", iteration=2,
                      unwinding=unwinding)
    assert [s for s, _t in run.held_tests] == [B, A]
    assert dict(run.held_tests)[B] == frozenset({"b"})
    assert run.target_tests == frozenset({"b", "c"})
    # NEGATIVE CONTROL: the order before was `sorted` over the bodies, which
    # puts the earlier file first whatever the tests say.
    assert [s for s, _t in sorted(run.held_tests)] == [A, B]


def test_inside_a_group_the_stub_that_holds_more_tests_goes_first() -> None:
    c = FnSite("src/b.rs", "must_use", "")
    held = {"t1": frozenset({A}), "t2": frozenset({c}), "t3": frozenset({c})}
    assert holder_order([A, c], held, {}) == [c, A]
    assert holder_order([A, c, B], held, {"t1": frozenset({B})}) == [B, c, A]


# --- the next sweep offers an unwinding-only stub (review 2026-11-09) --------


def test_a_stub_tests_only_unwind_through_is_offered_after_the_deferral() -> None:
    """When the fill that measured the unwinding stub did not write it (the
    share refusal of fill 2 on run `630503-f5ca6540`), the next sweep must
    still offer it. Its tests first fail elsewhere, so the first-failure
    deferral removes it."""
    from pipeline_core.domain.dispatch_rank import Service, UnitFacts, rank, take
    from pipeline_core.domain.held_stubs import offered_after_deferral
    a, b = FnLocator(A.file, A.fn_name), FnLocator(B.file, B.fn_name)
    holds = HeldStubs({"b": frozenset({a})})
    frontier = {a: {"b"}, b: {"b"}}
    active, deferred = holds.consumer_deferrals(frontier)
    # NEGATIVE CONTROL: the first-failure deferral alone drops the stub.
    assert b not in active and b in deferred
    offered, back = offered_after_deferral(frontier, active, holds.units | {b})
    assert back == {b} and offered[b] == {"b"} and offered[a] == {"b"}

    # Offered, it must also get a held slot: 18 first-failure holders fill
    # a cap of 12, and every unwinding stub needs its own place, not the one
    # spare slot the cap leaves for the rest.
    holders = [FnLocator(f"src/h{i}.rs", "f") for i in range(18)]
    b2 = FnLocator("src/y.rs", "drop")
    facts = {h: UnitFacts(static_index=i, service=Service.NEVER, held_tests=1)
             for i, h in enumerate(holders)}
    facts[b] = UnitFacts(static_index=18, service=Service.NEVER, held_tests=152)
    facts[b2] = UnitFacts(static_index=19, service=Service.NEVER, held_tests=40)
    ranked = rank(facts)
    batch = take(ranked, {*holders, b, b2}, 12, cohort=None, traversed=())
    assert b in batch.keys and b2 in batch.keys
    # NEGATIVE CONTROL: with only the first-failure holders held, one of
    # the two unwinding stubs is left out.
    old = take(ranked, set(holders), 12, cohort=None, traversed=())
    assert not (b in old.keys and b2 in old.keys)
