"""F4 pins — core-first dispatch (defer consumers blocked by stubbed producers)."""
from __future__ import annotations

from pipeline_core.domain.dispatch import DispatchClass, defer_blocked_consumers
from pipeline_core.domain.models import FnLocator

PROD = FnLocator("src/error.rs", "context")   # still-stubbed producer
CONS = FnLocator("src/lib.rs", "wrap")        # filled consumer of PROD


def test_pure_consumer_deferred_producer_active() -> None:
    frontier = {PROD: {"t_len"}, CONS: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert PROD in active and CONS not in active   # producer dispatched
    assert deferred == {CONS: frozenset({PROD})}   # consumer deferred + blocker


def test_consumer_with_unblocked_test_stays_active() -> None:
    # t_own is not owned by any stub → real work the model can do now.
    frontier = {PROD: {"t_len"}, CONS: {"t_len", "t_own"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert CONS in active and not deferred


def test_producer_never_deferred_even_if_blocking_itself() -> None:
    frontier = {PROD: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert active == frontier and not deferred


def test_readmitted_when_producer_no_longer_stub() -> None:
    # once PROD fills (leaves the stub set), the consumer is active again.
    frontier = {PROD: {"t_len"}, CONS: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset())
    assert CONS in active and not deferred


def test_dispatch_class_values() -> None:
    assert DispatchClass.ACTIVE is not DispatchClass.DEFERRED


# ---- impl-route coverage: which stubs that route will never attempt ----

from pipeline_core.domain.dispatch import (  # noqa: E402
    impl_route_covers,
    stubs_deferred_by_impl_route,
)
from pipeline_core.domain.models import FnRecord, ImplBlock  # noqa: E402


def _blk(container: str, stubs: tuple[str, ...] = (),
         refills: tuple[str, ...] = ()) -> ImplBlock:
    mk = tuple(FnRecord(file="src/e.rs", fn_name=n) for n in stubs)
    rf = tuple(FnRecord(file="src/e.rs", fn_name=n) for n in refills)
    return ImplBlock(file="src/e.rs", container=container,
                     members=mk + rf, stub_members=mk, refill_members=rf)


def test_impl_route_skips_free_fns_and_oversized_blocks() -> None:
    assert impl_route_covers(_blk("impl E", ("a",)), max_stubs=2)
    assert not impl_route_covers(_blk("", ("free",)), max_stubs=2)      # free fn
    assert not impl_route_covers(_blk("impl E"), max_stubs=2)           # no work
    # cap counts stubs + refills together, not stubs alone
    assert not impl_route_covers(_blk("impl E", ("a", "b"), ("c",)), max_stubs=2)


def test_deferred_stubs_are_exactly_the_uncovered_ones() -> None:
    blocks = [_blk("impl E", ("covered",)),
              _blk("", ("free1", "free2")),
              _blk("impl Big", ("x", "y", "z"))]
    got = stubs_deferred_by_impl_route(blocks, max_stubs=2)
    assert got == frozenset({FnLocator("src/e.rs", "free1"),
                             FnLocator("src/e.rs", "free2"),
                             FnLocator("src/e.rs", "x"),
                             FnLocator("src/e.rs", "y"),
                             FnLocator("src/e.rs", "z")})
    # a refill member of an uncovered block is NOT a stub → never deferred here
    assert stubs_deferred_by_impl_route(
        [_blk("", (), ("refill_only",))], max_stubs=2) == frozenset()


# ---- red-build narrowing: while the crate does not compile, only the fns
# ---- rustc names are worth an LLM call

from pipeline_core.domain.dispatch import restrict_to_compile_blockers  # noqa: E402

_LIB = FnLocator("src/error.rs", "construct")
_OTHER = FnLocator("src/context.rs", "ext_context")
_TEST_FN = FnLocator("src/error.gen_tests_a.rs", "test_downcast")


def test_unimplicated_frontier_keys_are_withheld_while_red() -> None:
    frontier = {_LIB: {"t1"}, _OTHER: {"t1", "t2"}}
    actionable, withheld = restrict_to_compile_blockers(
        frontier, frozenset({_LIB}))
    assert actionable == {_LIB: {"t1"}}     # tests preserved for the aimed fn
    assert withheld == frozenset({_OTHER})


def test_implicated_fn_absent_from_the_frontier_is_added_with_no_tests() -> None:
    """No test can name a fn while nothing runs — being unnamed is the
    condition, not a reason to skip it."""
    actionable, withheld = restrict_to_compile_blockers(
        {_LIB: {"t1"}}, frozenset({_OTHER}))
    assert actionable == {_OTHER: set()}
    assert withheld == frozenset({_LIB})


def test_added_keys_arrive_unfiltered_so_the_caller_must_refilter() -> None:
    """The contract the driver depends on: a diagnostic inside a gen_tests fn
    puts a TEST target in the result, because this function knows nothing about
    the caller's filters. Pinned so the re-filter at the call site is never
    dropped as redundant."""
    actionable, _ = restrict_to_compile_blockers(
        {_LIB: {"t1"}}, frozenset({_TEST_FN}))
    assert _TEST_FN in actionable


def test_nothing_implicated_yields_an_empty_frontier() -> None:
    actionable, withheld = restrict_to_compile_blockers(
        {_LIB: {"t1"}}, frozenset())
    assert actionable == {}
    assert withheld == frozenset({_LIB})
