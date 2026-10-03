"""Core-first dispatch (defer consumers blocked by stubbed producers)."""
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
    refills_deferred_by_impl_route,
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


def test_refills_of_uncovered_blocks_are_named_by_their_own_predicate() -> None:
    """Nothing named the re-fill targets of blocks the impl route declines, so a filled free fn implicated by a failing
    test was written by no route at all: the impl route declined it on the
    container, the per-fn route was handed stubs only, and the file-first plan
    listed impl blocks. On the live anyhow tree that was all 13 free fns of
    `src/error.rs`, holding the crate's remaining use-after-free."""
    free_refill = _blk("", (), ("object_downcast", "vtable"))
    covered = _blk("impl E", ("a",))
    oversized = _blk("impl Big", ("x",), ("y", "z"))
    got = refills_deferred_by_impl_route(
        [covered, free_refill, oversized], max_stubs=2)
    assert got == frozenset({FnLocator("src/e.rs", "object_downcast"),
                             FnLocator("src/e.rs", "vtable"),
                             FnLocator("src/e.rs", "y"),
                             FnLocator("src/e.rs", "z")})
    # stubs are the other predicate's business, never double-counted here
    assert FnLocator("src/e.rs", "a") not in got
    assert FnLocator("src/e.rs", "x") not in got


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


# ---- file-route unit selection: which files the file-first pass dispatches,
# ---- and how PHASE_C_ROUTE_ORDER=file_primary widens it

from pipeline_core.domain.dispatch import (  # noqa: E402
    file_route_units,
)


def _fblk(file: str, container: str, stubs: tuple[str, ...] = (),
          refills: tuple[str, ...] = ()) -> ImplBlock:
    mk = tuple(FnRecord(file=file, fn_name=n) for n in stubs)
    rf = tuple(FnRecord(file=file, fn_name=n) for n in refills)
    return ImplBlock(file=file, container=container,
                     members=mk + rf, stub_members=mk, refill_members=rf)


def test_default_takes_multiblock_files_and_refill_deferred_only() -> None:
    """The historical selection, pinned: a lone viable block is the per-block
    route's own unit (running it here too doubles its attempts); a free-fn
    block with only STUBS is the per-fn route's; a free-fn RE-fill joins its
    file because no other route can name it."""
    lone = _fblk("src/a.rs", "impl A", ("a1",))
    multi1 = _fblk("src/b.rs", "impl B", ("b1",))
    multi2 = _fblk("src/b.rs", "impl B2", ("b2",))
    free_stub = _fblk("src/c.rs", "", ("c_free",))
    free_refill = _fblk("src/d.rs", "", (), ("d_refill",))
    units = dict(file_route_units(
        [lone, multi1, multi2, free_stub, free_refill], max_stubs=2))
    assert "src/a.rs" not in units          # single viable block
    assert units["src/b.rs"] == [multi1, multi2]
    assert "src/c.rs" not in units          # stub-only free fn -> per-fn route
    assert units["src/d.rs"] == [free_refill]


def test_file_primary_takes_every_frontier_file() -> None:
    lone = _fblk("src/a.rs", "impl A", ("a1",))
    free_stub = _fblk("src/c.rs", "", ("c_free",))
    no_work = _fblk("src/e.rs", "impl E")
    units = dict(file_route_units(
        [lone, free_stub, no_work], max_stubs=2, file_primary=True))
    assert units["src/a.rs"] == [lone]      # single-block file now dispatches
    assert units["src/c.rs"] == [free_stub]  # stub-only free fn now dispatches
    assert "src/e.rs" not in units          # a block with no work never does


def test_file_units_keep_impl_headers_before_free_fn_blocks() -> None:
    """`containers[0]` is the file's routing target and must stay an impl
    header wherever the file has one — free-fn blocks are appended after the
    viable blocks in both modes, whatever order the caller supplied."""
    free = _fblk("src/x.rs", "", (), ("f1",))
    viable = _fblk("src/x.rs", "impl X", ("x1",))
    for primary in (False, True):
        units = dict(file_route_units([free, viable], max_stubs=2,
                                      file_primary=primary))
        assert units["src/x.rs"][0] is viable


# ---- sibling-blocked U2 failures: the post-fill counterpart of the
# ---- dispatch-time deferral (a hub's first body fails tests that also need
# ---- still-stubbed spokes — attempt-invariant, so the fix loop must not run)

from pipeline_core.domain.dispatch import (  # noqa: E402
    sibling_blocked_u2_failures,
)

HUB = FnLocator("src/error.rs", "error")
SPOKE = FnLocator("src/error.rs", "msg")
SPOKE2 = FnLocator("src/ptr.rs", "boxed")


def test_all_failures_held_by_stubbed_siblings_returns_the_blockers() -> None:
    dispatch = {HUB: {"t_a", "t_b"}, SPOKE: {"t_a"}, SPOKE2: {"t_b"}}
    held = sibling_blocked_u2_failures(
        frozenset({"t_a", "t_b"}), HUB, dispatch,
        frozenset({HUB, SPOKE, SPOKE2}))
    assert held == frozenset({SPOKE, SPOKE2})


def test_one_unheld_failure_means_the_unit_owns_it() -> None:
    # t_b is attributed to no still-stubbed sibling -> the unit's own failure;
    # the fix loop must run (returns None, not a partial blocker set).
    dispatch = {HUB: {"t_a", "t_b"}, SPOKE: {"t_a"}}
    assert sibling_blocked_u2_failures(
        frozenset({"t_a", "t_b"}), HUB, dispatch,
        frozenset({HUB, SPOKE})) is None


def test_the_unit_itself_never_counts_as_its_own_blocker() -> None:
    # HUB is still in the stub scan (it was a stub at pass start; the body
    # under judgment just landed) — its own attribution must not read as
    # "blocked by a sibling".
    dispatch = {HUB: {"t_a"}}
    assert sibling_blocked_u2_failures(
        frozenset({"t_a"}), HUB, dispatch, frozenset({HUB})) is None


def test_a_filled_sibling_no_longer_blocks() -> None:
    # SPOKE left the stub set (filled earlier in the pass) -> its test is
    # winnable now; the fix loop runs.
    dispatch = {HUB: {"t_a"}, SPOKE: {"t_a"}}
    assert sibling_blocked_u2_failures(
        frozenset({"t_a"}), HUB, dispatch, frozenset({HUB})) is None


def test_unattributed_and_empty_failures_are_the_units_own() -> None:
    # A test absent from the frontier attribution is unattributed evidence —
    # conservative direction is to fix, not defer. No failures: nothing to
    # defer either.
    dispatch = {HUB: {"t_a"}, SPOKE: {"t_a"}}
    stubs = frozenset({HUB, SPOKE})
    assert sibling_blocked_u2_failures(
        frozenset({"t_unknown"}), HUB, dispatch, stubs) is None
    assert sibling_blocked_u2_failures(frozenset(), HUB, dispatch, stubs) is None
