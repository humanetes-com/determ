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
    promote_crash_evidence,
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


def test_a_growing_frontier_never_closes_a_pass_without_a_cohort():
    """THE NEGATIVE CONTROL for the cohort, and the whole of run
    `1692702-94e6163a`'s failure to terminate.

    Its frontier grew 35 -> 57 -> 63 while the cursor advanced 12 -> 21 -> 36,
    because futility escalation adds a crashed test's producers and crash
    evidence adds its subjects. With closure judged against the frontier AS IT
    STANDS, the pending set never empties: `completes_pass` is False forever,
    `_traversals` stays 0, and BOTH terminators denominated in traversals — the
    flat-score guard and the plateau budget — are disabled by arithmetic."""
    sizes = [35, 57, 63, 69, 75]
    attempted: set[str] = set()
    completions: list[bool] = []
    for n in sizes:
        ordered = [f"u{i}" for i in range(n)]
        b = take_batch(ordered, attempted, 12)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert completions == [False] * len(sizes)


def test_the_cohort_closes_the_pass_even_while_the_frontier_grows():
    """The same growth, judged against the cohort the pass OPENED over: 35
    units at 12 a sweep closes on the third batch, and the units that arrived
    while it ran belong to the NEXT cohort rather than extending this one."""
    cohort = [f"u{i}" for i in range(35)]
    attempted: set[str] = set()
    completions: list[bool] = []
    for n in (35, 57, 63):
        ordered = [f"u{i}" for i in range(n)]
        b = take_batch(ordered, attempted, 12, cohort=cohort)
        completions.append(b.completes_pass)
        attempted = set() if b.completes_pass else attempted | set(b.keys)
    assert completions == [False, False, True]


def test_a_cohort_unit_that_left_the_frontier_does_not_block_closure():
    """A cohort member that filled and left can never be attempted again, so
    waiting for it would be the same unsatisfiable condition in a smaller
    disguise: closure asks about cohort units STILL in the frontier."""
    cohort = ["a", "b", "c"]
    b = take_batch(["a", "c"], {"a"}, 12, cohort=cohort)
    assert b.keys == ("c",)
    assert b.completes_pass


def test_cohort_none_is_exactly_the_pre_cohort_behaviour():
    """The default must not change for a caller that does not track passes —
    the cohort is then the frontier as it stands, which is what every existing
    case above asserts."""
    ordered = [f"u{i}" for i in range(30)]
    for attempted in (set(), {"u0", "u1"}, set(ordered[:29])):
        a = take_batch(ordered, attempted, 12)
        b = take_batch(ordered, attempted, 12, cohort=ordered)
        assert (a.keys, a.completes_pass) == (b.keys, b.completes_pass)


def test_crash_evidence_is_promoted_ahead_of_the_static_ranking():
    """The 180.2 defect: the evidence pack granted eligibility and the ranking
    decided WHEN, so the five fns a double drop named waited for their
    alphabetical turn. Promotion puts every graded unit ahead of the frontier,
    best grade first — an observed frame above an inference above a synthesized
    subject — and leaves the rest in the order `order_frontier` chose."""
    ordered = [(f"u{i}", {"t"}) for i in range(6)]
    out = promote_crash_evidence(ordered, {"u4": 1, "u5": 0, "u2": 2})
    assert [k for k, _ in out] == ["u5", "u4", "u2", "u0", "u1", "u3"]


def test_promotion_is_stable_so_one_fault_s_family_dispatches_together():
    """Within a grade the ranking is untouched, so the five subjects of one
    fault occupy consecutive slots of the batch that found them instead of
    arriving one iteration apart in name order — which is how
    `object_drop_front`, last of its family on the `(file, fn)` tiebreak, went
    undispatched while `object_drop` (correct on disk) took the slot."""
    fam = ["context_chain_drop_rest", "context_drop_rest", "drop",
           "object_drop", "object_drop_front"]
    # The frontier as run `1692702-94e6163a` had it: the family ranked behind
    # the units attribution had already collected, on a frontier well over the
    # cap of 12.
    ordered = [(f"u{i}", {"t"}) for i in range(20)] + [(k, {"t"}) for k in fam]
    grades = dict.fromkeys(fam, 1)
    # NEGATIVE CONTROL — the pre-fix ordering. Every one of the five is
    # eligible and none of them is dispatched: iteration 2 of that run took 12
    # of 57 and logged `0 skipped — free fns` while four of the five are free
    # fns.
    before = take_batch([k for k, _ in ordered], set(), 12).keys
    assert not set(before) & set(fam)
    out = promote_crash_evidence(ordered, grades)
    assert [k for k, _ in out][:5] == fam
    assert take_batch([k for k, _ in out], set(), 12).keys[:5] == tuple(fam)


def test_promotion_without_evidence_changes_nothing():
    """No crash this sweep must mean no reordering at all: the static ranking
    is correct for an attribution-built frontier and this function must not
    become a second opinion about it."""
    ordered = [(f"u{i}", {"t"}) for i in range(5)]
    assert promote_crash_evidence(ordered, {}) == ordered
    assert promote_crash_evidence(ordered, {"absent": 0}) == ordered


def test_rendered_marks_a_mid_pass_batch_for_the_iteration_line():
    """The iteration line is the one place a live run's cadence is visible, and
    the census keys its P7 reaction marker on the mid-pass text."""
    ordered = [f"u{i}" for i in range(30)]
    assert "mid-pass" in take_batch(ordered, set(), 12).rendered
    assert "mid-pass" not in take_batch(ordered, set(), 0).rendered
