"""Pins for the BDD behaviour oracle domain (F10)."""
from __future__ import annotations

import pytest

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    BehaviourVerdict,
    FenceDirective,
    ObservableOutcome,
    OutcomeKind,
    StaticAssertKind,
    union_cluster_suites,
)


def test_outcome_sum_rejects_wrong_payload():
    # STATIC_ASSERTION must carry a static_kind; runtime kinds must not.
    with pytest.raises(ValueError):
        ObservableOutcome(OutcomeKind.STATIC_ASSERTION)
    with pytest.raises(ValueError):
        ObservableOutcome(OutcomeKind.DISPLAY_STRING,
                          static_kind=StaticAssertKind.SIZE)
    with pytest.raises(ValueError):
        ObservableOutcome(OutcomeKind.CHAIN_SEQUENCE)  # empty chain


def test_outcome_roundtrip_every_variant():
    outs = [
        ObservableOutcome.display("boom"),
        ObservableOutcome.debug("Error { .. }"),
        ObservableOutcome.chain_seq(("a", "b")),
        ObservableOutcome.downcast_ok(True),
        ObservableOutcome.panic("assertion failed"),
        ObservableOutcome.static_assertion(StaticAssertKind.SIZE,
                                           "size_of::<Error>() == size_of::<usize>()"),
    ]
    for o in outs:
        assert ObservableOutcome.from_dict(o.to_dict()) == o


def test_is_runtime_partitions_static_from_runtime():
    assert ObservableOutcome.display("x").is_runtime
    assert not ObservableOutcome.static_assertion(
        StaticAssertKind.AUTOTRAITS, "Send + Sync").is_runtime


def test_positive_oracle_gate():
    disp = ObservableOutcome.display("boom")
    # a plain runnable outcome-behaviour is a positive oracle.
    b = BehaviourSpec("b1", "let e = anyhow!(\"boom\");", "e.to_string()", disp,
                      subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN)
    assert b.is_positive_oracle
    # compile_fail is a NEGATIVE behaviour — never a positive oracle.
    assert not BehaviourSpec("b2", "", "", disp, fence=FenceDirective.COMPILE_FAIL,
                             subkind=BehaviourSubKind.OUTCOME).is_positive_oracle
    # ignore is skipped (assertion-behaviour carries no `then`).
    assert not BehaviourSpec("b3", "", "", None, fence=FenceDirective.IGNORE,
                             subkind=BehaviourSubKind.ASSERTION).is_positive_oracle
    # an outcome-behaviour pinning a STATIC assertion is not a runtime oracle.
    stat = ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "…")
    assert not BehaviourSpec("b4", "", "", stat, subkind=BehaviourSubKind.OUTCOME,
                             fence=FenceDirective.RUN).is_positive_oracle


def test_behaviour_spec_roundtrip():
    # assertion-behaviour: no `then` (the assert IS the check).
    b = BehaviourSpec(
        "err_display", 'let e = anyhow!("boom");', "e.to_string()", None,
        exercises=frozenset({"src/lib.rs::anyhow", "src/fmt.rs::fmt"}),
        provenance=BehaviourProvenance.HARVESTED,
        subkind=BehaviourSubKind.ASSERTION, fence=FenceDirective.RUN,
        cfg_cluster="stable", source_ref="src/lib.rs:42")
    assert BehaviourSpec.from_dict(b.to_dict()) == b
    # outcome-behaviour: carries the golden Then, roundtrips too.
    o = BehaviourSpec("err_out", "", "e.to_string()",
                      ObservableOutcome.display("boom"),
                      subkind=BehaviourSubKind.OUTCOME)
    assert BehaviourSpec.from_dict(o.to_dict()) == o


def test_suite_filters_positive_oracle_specs_and_roundtrips():
    disp = ObservableOutcome.display("x")
    good = BehaviourSpec("g", "", "", disp, subkind=BehaviourSubKind.OUTCOME)
    ignored = BehaviourSpec("i", "", "", None, fence=FenceDirective.IGNORE)
    suite = BehaviourSuite("stable", (good, ignored))
    assert suite.positive_oracle_specs == (good,)
    assert BehaviourSuite.from_dict(suite.to_dict()) == suite


def test_verdict_enum_present():
    assert {BehaviourVerdict.HOLD, BehaviourVerdict.MISMATCH}


def test_union_cluster_suites_keeps_divergent_thens_regardless_of_glob_order():
    """First-wins on behaviour_id would take nightly's Then: sorted glob
    is `_behaviours_nightly.json` then `_behaviours_stable.json`. A shared
    id whose Then differs is two observables — both stay. Same Then is a
    duplicate. Negative control is first-wins-on-id on the same pair."""
    nightly = BehaviourSuite("nightly", (
        BehaviourSpec(
            "id1", "", "err.is::<&str>()",
            ObservableOutcome.type_identity("&str", True),
            subkind=BehaviourSubKind.OUTCOME),
    ))
    stable = BehaviourSuite("stable", (
        BehaviourSpec(
            "id1", "", "err.is::<String>()",
            ObservableOutcome.type_identity("String", True),
            subkind=BehaviourSubKind.OUTCOME),
    ))

    def _first_wins_on_id(suites):
        seen: set[str] = set()
        out = []
        for suite in suites:
            for spec in suite.specs:
                if spec.behaviour_id in seen:
                    continue
                seen.add(spec.behaviour_id)
                out.append(spec)
        return out

    night_first = (nightly, stable)
    assert [s.then.value for s in _first_wins_on_id(night_first)
            if s.then is not None] == ["&str"]
    assert {s.then.value for s in union_cluster_suites(night_first)
            if s.then is not None} == {"&str", "String"}
    assert {s.then.value for s in union_cluster_suites((stable, nightly))
            if s.then is not None} == {"&str", "String"}

    dup = BehaviourSuite("stable", (
        BehaviourSpec(
            "id1", "", "err.is::<&str>()",
            ObservableOutcome.type_identity("&str", True),
            subkind=BehaviourSubKind.OUTCOME),
    ))
    assert len(union_cluster_suites((nightly, dup))) == 1
