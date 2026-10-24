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

from typing import Any

from pipeline_core.domain.attribution import (
    FailureFrontier,
    FailureKind,
    canon_site,
    kind_after_sight_filter,
)
from pipeline_core.domain.models import FnLocator, FnSite

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
    assert not FailureKind.OBSERVED.convicts


def test_a_guessing_kind_that_lost_edges_is_labelled_observed() -> None:
    """The dispatch label after the sight filter. A classifier, recency or
    reverse-map frontier that lost an edge is no longer that guess. A
    frontier that already observed the failure, a merged one, and an
    injected empty frontier keep their labels even when an edge left."""
    for kind in (FailureKind.CLASSIFIER, FailureKind.RECENTLY_FILLED,
                 FailureKind.REVERSE_MAP):
        assert kind_after_sight_filter(kind, dropped=True) is FailureKind.OBSERVED
        assert kind_after_sight_filter(kind, dropped=False) is kind
    for kind in (FailureKind.RUNTIME, FailureKind.COMPILE, FailureKind.BEHAVIOR,
                 FailureKind.MERGED, FailureKind.NO_FRONTIER,
                 FailureKind.CRASHED_BINARY, FailureKind.OBSERVED):
        assert kind_after_sight_filter(kind, dropped=True) is kind


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
    """`convicting ⊆ dispatch` per key, and a key dropped from dispatch (the
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
    assert FailureFrontier.of_dispatch(dispatch).sources == {}


def test_sources_travel_on_the_frontier_and_match_bare_names() -> None:
    """Test-own source rides beside evidence; homonym scoring reads this."""
    dispatch = {A: {"test_rev"}}
    convicting = {A: {"test_rev"}}
    fr = FailureFrontier.build(dispatch, convicting, sources={
        "chain::test_rev": 'fn test_rev() { format!("{:?}", err); }',
    })
    assert "{:?}" in fr.source_for("test_rev")
    assert fr.source_for("chain::test_rev") == fr.source_for("test_rev")
    assert FailureFrontier.build(dispatch, convicting).source_for("test_rev") == ""


def test_build_lifts_locator_convicting_to_empty_container_site() -> None:
    """of_direct / locator-keyed build still stamps via convicting_for."""
    fr = FailureFrontier.build({A: {"t1"}}, {A: {"t1"}})
    assert fr.convicting_for(A) == {"t1"}
    from pipeline_core.domain.models import FnSite
    assert FnSite(A.file, A.fn_name, "") in fr.convicting


def test_site_tests_are_subset_of_dispatch_locator() -> None:
    """Every test on a site is in dispatch[FnLocator(file, name)]."""
    from pipeline_core.domain.models import FnSite
    from_cont = "impl From<Error> for Box<dyn StdError>"
    boxed = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    from_site = FnSite("src/error.rs", "from", from_cont)
    loc_boxed = FnLocator("src/error.rs", "construct_from_boxed")
    loc_from = FnLocator("src/error.rs", "from")
    fr = FailureFrontier.build(
        {loc_boxed: {"test_boxed_str"}, loc_from: {"test_boxed_str", "dbg"}},
        {boxed: {"test_boxed_str"}, from_site: {"dbg"}},
    )
    for site, tests in fr.convicting.items():
        loc = FnLocator(site.file, site.fn_name)
        assert tests <= fr.dispatch[loc]


def test_runtime_site_does_not_convict_from_homonym() -> None:
    """When only the construct_from_boxed SITE carries test_boxed_str,
    convicting_for(from) is empty — the previous locator map put that test
    on every ``from``."""
    from pipeline_core.domain.models import FnSite
    boxed = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    from_site = FnSite(
        "src/error.rs", "from",
        "impl From<Error> for Box<dyn StdError + Send + 'static>")
    loc_boxed = FnLocator(boxed.file, boxed.fn_name)
    loc_from = FnLocator(from_site.file, from_site.fn_name)
    fr = FailureFrontier.build(
        {loc_boxed: {"test_boxed_str"}, loc_from: {"test_boxed_str"}},
        {boxed: {"test_boxed_str"}},
    )
    assert "test_boxed_str" in fr.convicting_for_site(boxed)
    assert not fr.convicting_for_site(from_site)
    assert "test_boxed_str" not in fr.convicting_for(loc_from)
    assert "test_boxed_str" in fr.convicting_for(loc_boxed)


def test_extract_tdd_frontier_writes_the_located_site(monkeypatch: Any) -> None:
    """RUNTIME write: a stub panic inside construct_from_boxed convicts
    that site only. Previously the same panic sat on FnLocator(..., 'from')
    because make_fn_locator dropped the container."""
    from pathlib import Path
    from types import SimpleNamespace

    import pipeline_core.application.reconstruct.attribution as attr

    boxed = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    monkeypatch.setattr(
        attr, "make_fn_site_locator",
        lambda *_a, **_k: lambda _f, _l: boxed)
    output = (
        "test test_boxed_str ... FAILED\n\n"
        "failures:\n\n"
        "---- test_boxed_str stdout ----\n"
        "thread 'test_boxed_str' panicked at src/error.rs:287:9:\n"
        "not implemented\n\n"
        "failures:\n    test_boxed_str\n"
    )
    dispatch, sites = attr.extract_tdd_frontier(
        SimpleNamespace(structure=SimpleNamespace()), output, Path("/x"))
    want = canon_site(boxed)
    loc = FnLocator(want.file, want.fn_name)
    assert dispatch[loc] == {"test_boxed_str"}
    assert sites[want] == {"test_boxed_str"}
    assert not any(s.fn_name == "from" for s in sites)


def test_filter_test_targets_drops_oracle_sites() -> None:
    """Conviction is a SiteFrontier; oracle keys drop by (file, name)
    without collapsing the surviving site back to a locator."""
    from pipeline_core.application.reconstruct.test_targets import (
        filter_test_targets,
    )
    prod = FnSite("src/lib.rs", "cap_limit", "impl Foo")
    oracle = FnSite("src/lib.rs", "extra_cap_limit", "mod tests")
    recs = [
        {"file": "src/lib.rs", "fn_name": "cap_limit", "cfg_predicates": []},
        {"file": "src/lib.rs", "fn_name": "extra_cap_limit",
         "cfg_predicates": ["test"], "container": "mod tests"},
    ]
    out = filter_test_targets({prod: {"t1"}, oracle: {"t2"}}, recs)
    assert prod in out and oracle not in out
    assert out[prod] == {"t1"}
