"""Stage 1's assertion: the loop's state vector is a fold over its own facts.

The gate on the whole programme is completeness — a fold over an incomplete log
is worse than a mutable local, because it reads a default where the variable
would have read the truth. `scripts/fold_completeness.py` runs that check
against the real event store; these tests pin the fold's rules on streams small
enough to read, including the two shapes the recorded runs actually contain: an
iteration that measured but dispatched nothing (a retrigger's `continue`) and
an iteration whose batch closed the pass.
"""
from __future__ import annotations

from pipeline_core.domain.loop_facts import (
    BatchDispatched,
    BodiesReseeded,
    BodiesRestubbed,
    PassClosed,
    SweepMeasured,
    fold,
    judged_readings,
)


def _sweep(it: int, p: int, o: int, **kw: object) -> SweepMeasured:
    return SweepMeasured(iter=it, cluster="stable", pass_count=p,
                         fail_count=o - p, oracle_size=o,
                         **kw)  # type: ignore[arg-type]


def test_the_fold_rebuilds_one_iteration_from_its_three_facts() -> None:
    """One printed line is a JOIN of three things that happen at three points
    in the iteration — the sweep, the batch, and the pass closure. The fold is
    where they are put back together."""
    view = fold([
        _sweep(1, 83, 105, rc=101, elapsed_s=0.14),
        BatchDispatched(iter=1, cluster="stable", frontier_size=28,
                        dispatched=12, attempted_before=0, cohort_size=28,
                        skipped_exhausted=3),
    ], max_stalls=0)
    assert len(view.vectors) == 1
    v = view.vectors[0]
    assert (v.pass_count, v.fail_count, v.oracle_size) == (83, 22, 105)
    assert (v.frontier_size, v.dispatched, v.skipped_exhausted) == (28, 12, 3)
    assert v.render() == (
        "[phase-c/tdd] iter 1: cargo test rc=101 (0.1s); pass=83 fail=22 "
        "oracle=105 (improved=True, stall=0/0); frontier=28 fn(s) (behavior), "
        "3 skipped as exhausted; dispatching 12 of 28")


def test_an_iteration_that_dispatched_nothing_still_produces_a_vector() -> None:
    """The retrigger's `continue` commits no batch, and the loop still printed
    an iteration. A fold that silently skipped it would disagree with the log
    on the ITERATION COUNT, which is the first thing an operator reads — and
    the recorded runs contain exactly this shape (`1920662-40f711b2` iter 4,
    `3124988-ac6886ba` iter 7)."""
    view = fold([_sweep(1, 83, 105), _sweep(2, 83, 105, judged=False)])
    assert [v.iter for v in view.vectors] == [1, 2]
    assert view.vectors[1].dispatched == 0


def test_improved_and_the_stall_streak_are_derived_not_carried() -> None:
    view = fold([_sweep(1, 83, 105), _sweep(2, 83, 105), _sweep(3, 92, 105)])
    assert [v.improved for v in view.vectors] == [True, False, True]
    assert view.best_pass == 92


def test_an_unjudged_sweep_cannot_move_the_high_water() -> None:
    """A grace sweep after a deliberate re-stub measures the destruction, not
    the fills. It is not evidence in either direction."""
    view = fold([_sweep(1, 83, 105), _sweep(2, 91, 105, judged=False)])
    assert view.best_pass == 83
    assert [v.improved for v in view.vectors] == [True, False]


def test_traversals_are_counted_out_of_the_stream() -> None:
    """The driver used to hand this number to the guard from a local, so a gate
    both counted and decided. Here the count is a fold over pass closures and
    the gate only asks."""
    facts = [
        _sweep(1, 83, 105),
        PassClosed(iter=1, cluster="stable", cohort_size=43, attempted=43),
        _sweep(2, 83, 105),
    ]
    view = fold(facts)
    assert view.traversals == 1
    assert [t for _p, _o, t in judged_readings(facts)] == [0, 1]


def test_a_destruction_and_its_undo_are_both_facts() -> None:
    """`bodies_reseeded` is new since the original analysis and is why the
    vocabulary had to be re-opened: a destruction UNDONE inside the iteration
    changes what every destruction-sensitive exemption observes."""
    view = fold([
        _sweep(1, 21, 105, judged=False),
        BodiesRestubbed(iter=1, cluster="stable", count=56, cause="retrigger"),
        BodiesReseeded(iter=1, cluster="stable", seeded=50, rejected=1,
                       skipped=5, site="after retrigger re-stub"),
    ])
    assert view.vectors[0].restubbed == 56
    assert view.vectors[0].reseeded == 50
