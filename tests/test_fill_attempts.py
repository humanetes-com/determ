"""A unit is reverted at the end of a fill only if its LAST attempt was.

The sentences are the bookkeeping's contracts (`domain/fill_attempts.py`),
each with the pre-fix rule (`sticky_reverts=True`: a revert mark is never
cleared) as its control where one applies. The recorded instance — run
`1577437-c93d5c26` iter 4, `file src/error.rs` reverted on attempt 1 and
kept on attempt 2 — is pinned on its fixture in
`tests/test_fill_schedule_recorded.py`; the shapes here carry no live names.
"""
from __future__ import annotations

from pipeline_core.domain.fill_attempts import (
    AttemptOutcome,
    AttemptsSnapshot,
    FillAttempts,
)
from pipeline_core.domain.models import FnLocator

_A = FnLocator("src/a.rs", "one")
_B = FnLocator("src/a.rs", "two")
_C = FnLocator("src/b.rs", "three")


def test_a_keep_after_a_revert_of_the_same_units_is_a_keep() -> None:
    fa = FillAttempts()
    before = fa.snapshot()
    fa.note_dispatched([_A, _B])
    fa.note_reverted([_A, _B])          # attempt 1: compile failed
    fa.note_kept([_A, _B])              # attempt 2: kept
    delta = FillAttempts.since(before, fa.snapshot())
    assert delta.kept == {_A, _B} and not delta.reverted and not delta.lost
    assert fa.reverted == frozenset() and fa.dispatched == {_A, _B}
    # Negative control: the pre-fix rule reads the same sequence as reverted.
    old = FillAttempts(sticky_reverts=True)
    before = old.snapshot()
    old.note_dispatched([_A, _B])
    old.note_reverted([_A, _B])
    old.note_kept([_A, _B])
    delta = FillAttempts.since(before, old.snapshot())
    assert delta.reverted == {_A, _B} and not delta.kept
    assert old.reverted == {_A, _B}


def test_a_revert_after_a_keep_is_a_revert() -> None:
    fa = FillAttempts()
    fa.note_dispatched([_A])
    fa.note_kept([_A])
    fa.note_reverted([_A])
    assert fa.reverted == {_A}
    delta = FillAttempts.since(AttemptsSnapshot(), fa.snapshot())
    assert delta.reverted == {_A} and not delta.kept


def test_a_lost_answer_never_overrides_a_judgment_and_a_dispatch_clears_it() -> None:
    fa = FillAttempts()
    fa.note_dispatched([_A, _B, _C])
    fa.note_reverted([_A])
    fa.note_kept([_B])
    fa.note_lost([_A, _B, _C])          # a retry's empty reply
    assert fa.lost == {_C} and fa.reverted == {_A}
    delta = FillAttempts.since(AttemptsSnapshot(), fa.snapshot())
    assert delta.kept == {_B} and delta.reverted == {_A} and delta.lost == {_C}
    # A later route's real attempt supersedes the loss; a judgment stands.
    fa.note_dispatched([_A, _C])
    assert fa.lost == frozenset() and fa.reverted == {_A}
    # ...and a revert after the loss is a real attempt, never lost.
    fa.note_lost([_C])
    fa.note_reverted([_C])
    assert fa.lost == frozenset() and _C in fa.reverted


def test_a_dispatched_unit_no_attempt_judged_counts_as_kept() -> None:
    fa = FillAttempts()
    before = fa.snapshot()
    fa.note_dispatched([_A])
    delta = FillAttempts.since(before, fa.snapshot())
    assert delta.dispatched == {_A} and delta.kept == {_A}


def test_since_reads_only_what_the_run_touched_by_its_outcome_now() -> None:
    fa = FillAttempts()
    fa.note_dispatched([_A, _B])
    fa.note_reverted([_A])              # an earlier run reverted A
    fa.note_kept([_B])
    before = fa.snapshot()
    fa.note_dispatched([_A, _C])        # this run re-dispatches A, opens C
    fa.note_reverted([_A])              # reverted again
    fa.note_kept([_C])
    delta = FillAttempts.since(before, fa.snapshot())
    # The old diff of two monotone sets saw neither A's re-dispatch nor its
    # second revert (both already in the sets before the run) and reported
    # nothing for it; by its outcome now it is this run's reverted unit.
    assert delta.dispatched == {_A, _C}
    assert delta.reverted == {_A} and delta.kept == {_C}
    assert _B not in delta.kept         # untouched by this run
    # A keep of a unit no run dispatched (a whole-file passenger) is a keep.
    before = fa.snapshot()
    fa.note_kept([FnLocator("src/b.rs", "passenger")])
    delta = FillAttempts.since(before, fa.snapshot())
    assert delta.kept == {FnLocator("src/b.rs", "passenger")} and not delta.dispatched


def test_outcome_names_are_the_ledger_words() -> None:
    assert [o.value for o in AttemptOutcome] == ["kept", "reverted", "lost"]
