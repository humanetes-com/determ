"""Domain pins for the TDD dispatch batch.

The batch exists because the sweep is the loop's only measurement of the tree
and it was being taken once every 3-4 hours — one or two readings per run, so
every sweep-boundary lever (batch ratchet, re-admission, crash retrigger,
plateau exit) got one or two chances to fire. See
`pipeline_core/domain/dispatch_batch.py` for the measurement.

Two invariants carry the weight here and each has its own case below: the batch
is a PREFIX of the ranked frontier (so capping changes when the loop looks, not
what it fills or in what order), and the cursor is a SET (so a frontier that
churns between iterations cannot re-serve its head and starve its tail).
"""
from __future__ import annotations

from pipeline_core.domain.dispatch_batch import (
    DEFAULT_MAX_DISPATCH,
    take_batch,
)


def test_batch_is_a_prefix_of_the_ranking_and_does_not_complete_the_pass():
    """The cap withholds work without reordering it: `order_frontier` ranks
    leaf-first and the batch must take that ranking's head, or the cap would
    silently become a scheduling change."""
    ordered = [f"u{i}" for i in range(30)]
    b = take_batch(ordered, set(), 12)
    assert b.keys == tuple(ordered[:12])
    assert not b.completes_pass
    assert b.capped
    assert b.attempted_before == 0
    assert b.frontier_size == 30


def test_successive_batches_traverse_the_whole_frontier_then_close_the_pass():
    """Three batches of 12 cover 30 units, and only the LAST reports a
    completed pass — that flag is what lets the stall budget charge, so a
    mid-pass batch claiming it would restore the premature-exit bug."""
    ordered = [f"u{i}" for i in range(30)]
    attempted: set[str] = set()
    seen: list[str] = []
    completions: list[bool] = []
    for _ in range(3):
        b = take_batch(ordered, attempted, 12)
        seen.extend(b.keys)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert seen == ordered            # every unit dispatched exactly once
    assert completions == [False, False, True]
    # The pass closed, so the cursor is empty and the next pass reopens whole.
    assert take_batch(ordered, attempted, 12).attempted_before == 0


def test_the_cursor_is_a_set_so_a_churning_frontier_cannot_starve_its_tail():
    """The frontier is rebuilt every iteration and shrinks as fills land. A
    POSITIONAL cursor would re-serve the head each time the list changed; the
    attempted-set survives the churn and the tail still gets its turn."""
    first = [f"u{i}" for i in range(20)]
    b1 = take_batch(first, set(), 5)
    attempted = set(b1.keys)
    # Two of the dispatched units filled and left the frontier; one new unit
    # arrived from the freshly attributed failures.
    second = [k for k in first if k not in {"u0", "u1"}] + ["new"]
    b2 = take_batch(second, attempted, 5)
    assert not set(b2.keys) & attempted        # nothing re-served
    assert b2.keys == ("u5", "u6", "u7", "u8", "u9")
    # The unit that arrived mid-pass is still reachable before the pass closes.
    rest: set[str] = set(attempted) | set(b2.keys)
    later: list[str] = []
    for _ in range(4):
        b = take_batch(second, rest, 5)
        later.extend(b.keys)
        if b.completes_pass:
            break
        rest |= set(b.keys)
    assert "new" in later


def test_a_frontier_under_the_cap_completes_in_one_batch():
    """A small crate must not pay for the machinery: when the whole pending
    set fits, the batch is the frontier and the pass closes immediately —
    which is exactly the uncapped loop's behaviour."""
    b = take_batch(["a", "b", "c"], set(), DEFAULT_MAX_DISPATCH)
    assert b.keys == ("a", "b", "c")
    assert b.completes_pass
    assert not b.capped


def test_cap_of_zero_disables_batching():
    """`PHASE_C_TDD_MAX_DISPATCH=0` is the escape hatch back to pre-cap
    behaviour, and it must dispatch the WHOLE frontier in one completed pass —
    otherwise the escape hatch would still charge the stall differently."""
    ordered = [f"u{i}" for i in range(50)]
    b = take_batch(ordered, set(), 0)
    assert b.keys == tuple(ordered)
    assert b.completes_pass


def test_a_frontier_that_shrank_below_the_cursor_reopens_the_pass():
    """Every pending unit filled and left. There is nothing left to traverse,
    so the pass has closed: reopen on the frontier as it stands NOW rather
    than returning an empty batch forever (which would dispatch nothing and
    look like a plateau)."""
    b = take_batch(["a", "b"], {"a", "b", "gone"}, 12)
    assert b.keys == ("a", "b")
    assert b.completes_pass
    assert b.attempted_before == 0


def test_rendered_marks_a_mid_pass_batch_for_the_iteration_line():
    """The iteration line is the one place a live run's cadence is visible, and
    the census keys its P7 reaction marker on the mid-pass text."""
    ordered = [f"u{i}" for i in range(30)]
    assert "mid-pass" in take_batch(ordered, set(), 12).rendered
    assert "mid-pass" not in take_batch(ordered, set(), 0).rendered
