"""Pins for `FailureFrontier` — the split between the units a failing sweep
DISPATCHES and the tests allowed to REVERT them.

The plateau this exists to prevent: futility escalation added ~50 producer fns
per unresolved test, the same map was the U2 acceptance oracle, so every impl
block in the crate was convicted by tests it could not influence (an `ensure.rs`
tuple-Debug impl reverted for `error::…::test_from_boxed_basic`). 74 attempts,
0 fills, 5 GPU-hours — and the failure mode is attempt-invariant, so no retry
budget could ever have escaped it.
"""
from __future__ import annotations

from pipeline_core.domain.attribution import FailureFrontier, FailureKind
from pipeline_core.domain.models import FnLocator

A = FnLocator("src/a.rs", "producer")
B = FnLocator("src/b.rs", "accessor")


def test_only_observed_paths_convict() -> None:
    assert FailureKind.RUNTIME.convicts
    assert FailureKind.COMPILE.convicts
    assert FailureKind.BEHAVIOR.convicts
    assert not FailureKind.CLASSIFIER.convicts
    assert not FailureKind.RECENTLY_FILLED.convicts
    assert not FailureKind.REVERSE_MAP.convicts
    assert not FailureKind.NO_FRONTIER.convicts


def test_escalated_unit_is_dispatched_but_never_convicted() -> None:
    """Escalation widens the work list only. The escalated producer is attempted
    (it is in `dispatch`) and is judged on compiling + not regressing — never on
    the escalated test still failing."""
    dispatch = {A: {"t_futile"}, B: {"t_direct", "t_futile"}}
    convicting = {B: {"t_direct"}}      # only B's own body named t_direct
    fr = FailureFrontier.build(dispatch, convicting)
    assert fr.tests_for(A) == {"t_futile"}
    assert fr.convicting_for(A) == set()
    assert fr.convicting_for(B) == {"t_direct"}


def test_build_cannot_convict_beyond_what_it_dispatches() -> None:
    """`convicting ⊆ dispatch` per key, and a key dropped from dispatch (D3
    exhaustion filter, blocked-consumer deferral) takes its conviction with it —
    otherwise a filtered-out unit could still be reverted for someone's test."""
    fr = FailureFrontier.build(
        {A: {"t1"}},
        {A: {"t1", "t2"}, B: {"t3"}})
    assert fr.convicting_for(A) == {"t1"}
    assert fr.convicting_for(B) == set()
    assert B not in fr.dispatch


def test_unattributed_stub_dispatches_with_no_oracle() -> None:
    """A stub no failing test attributes carries an EMPTY test set: it must be
    filled, and the U2 gate has nothing to reject it with."""
    fr = FailureFrontier.build({A: set()}, {})
    assert A in fr.dispatch
    assert fr.convicting_for(A) == set()
    assert bool(fr)


def test_of_direct_and_of_dispatch_are_the_two_extremes() -> None:
    d = {A: {"t"}}
    assert FailureFrontier.of_direct(d).convicting_for(A) == {"t"}
    assert FailureFrontier.of_dispatch(d).convicting_for(A) == set()
    assert FailureFrontier.of_dispatch(d).tests_for(A) == {"t"}
    assert not FailureFrontier.of_dispatch({})


def test_evidence_travels_on_the_frontier_and_matches_bare_names() -> None:
    """The failing tests' own words ride the frontier (the one value every
    fill seam already receives). The escalation names tests bare (`test_rev`)
    while libtest prints `chain::test_rev`; the lookup matches on the last
    `::` segment, exact name first."""
    dispatch = {A: {"test_rev", "t_direct"}}
    convicting = {A: {"t_direct"}}
    fr = FailureFrontier.build(dispatch, convicting, evidence={
        "chain::test_rev": "panicked at tests/test_chain.rs:31\nleft: \"0\"\nright: \"3\"",
        "t_direct": "panicked at src/a.rs:1",
    })
    assert fr.evidence_for("test_rev").startswith("panicked at tests/test_chain.rs:31")
    assert fr.evidence_for("chain::test_rev") == fr.evidence_for("test_rev")
    assert fr.evidence_for("t_direct") == "panicked at src/a.rs:1"
    assert fr.evidence_for("never_ran") == ""
    # Default: no evidence, the projections untouched.
    bare = FailureFrontier.build(dispatch, convicting)
    assert bare.evidence == {} and bare.evidence_for("test_rev") == ""
    assert bare.tests_for(A) == {"test_rev", "t_direct"}
    assert FailureFrontier.of_dispatch(dispatch).evidence == {}
