"""Pins for the behaviour vocabulary added 2026-08-26: TYPE_IDENTITY /
ALT_DEBUG_STRING outcomes, BehaviourClass + classify_behaviour, the MAY note,
and merge_suites (existing wins)."""
from __future__ import annotations

import pytest

from pipeline_core.domain.behaviour import (
    MAY_REPRESENTATION_NOTE,
    RUNTIME_OUTCOME_KINDS,
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
    OutcomeKind,
    StaticAssertKind,
    classified,
    classify_behaviour,
    merge_suites,
)


def _outcome_spec(then: ObservableOutcome, *, when: str = "x", given: str = "",
                  cfg_cluster: str = "stable") -> BehaviourSpec:
    return BehaviourSpec("b", given, when, then, subkind=BehaviourSubKind.OUTCOME,
                         cfg_cluster=cfg_cluster)


def test_new_kinds_are_runtime_and_roundtrip():
    assert OutcomeKind.TYPE_IDENTITY in RUNTIME_OUTCOME_KINDS
    assert OutcomeKind.ALT_DEBUG_STRING in RUNTIME_OUTCOME_KINDS
    ti = ObservableOutcome.type_identity("&str", True)
    ad = ObservableOutcome.alt_debug("Custom {\n    kind: PermissionDenied,\n}")
    assert ti.is_runtime and ad.is_runtime
    assert ObservableOutcome.from_dict(ti.to_dict()) == ti
    assert ObservableOutcome.from_dict(ad.to_dict()) == ad
    assert ti.value == "&str" and ti.ok is True


def test_type_identity_requires_a_type_name():
    with pytest.raises(ValueError):
        ObservableOutcome.type_identity("  ", True)
    with pytest.raises(ValueError):
        ObservableOutcome(OutcomeKind.TYPE_IDENTITY)


def test_may_note_wording_is_stable():
    assert MAY_REPRESENTATION_NOTE == (
        "The layout, allocation strategy and private names are yours (MAY); "
        "every behaviour listed is a MUST.")


def test_klass_serialises_by_name_and_defaults_other_on_old_records():
    spec = classified(_outcome_spec(ObservableOutcome.type_identity("&str", True)))
    d = spec.to_dict()
    assert d["klass"] == "IDENTITY"
    assert BehaviourSpec.from_dict(d) == spec
    d.pop("klass")                      # a record written before the field
    assert BehaviourSpec.from_dict(d).klass is BehaviourClass.OTHER


def test_classify_precedence_table():
    static = ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<E>()")
    assert classify_behaviour(_outcome_spec(static)) is BehaviourClass.LAYOUT_PIN
    ident = ObservableOutcome.type_identity("&str", True)
    assert classify_behaviour(_outcome_spec(ident, when="e.downcast::<&str>()")) \
        is BehaviourClass.IDENTITY
    assert classify_behaviour(_outcome_spec(ident, when="(&*e as &dyn Any).type_id()")) \
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    assert classify_behaviour(_outcome_spec(ObservableOutcome.display("boom"))) \
        is BehaviourClass.EXACT_FORMAT
    assert classify_behaviour(_outcome_spec(ObservableOutcome.alt_debug("X {\n}"))) \
        is BehaviourClass.EXACT_FORMAT
    bounded = _outcome_spec(ObservableOutcome.display("aaa"),
                            given='let s = "a".repeat(3);\nassert_eq!(s.len(), 3);')
    assert classify_behaviour(bounded) is BehaviourClass.BOUNDED_RESOURCE
    commented = _outcome_spec(ObservableOutcome.display("x"),
                              given="// the message is capped at the limit\nlet s = f();")
    assert classify_behaviour(commented) is BehaviourClass.BOUNDED_RESOURCE
    assert classify_behaviour(_outcome_spec(ObservableOutcome.rendered("x"),
                                            when="e.source().is_none()")) \
        is BehaviourClass.OPTION_SEMANTICS
    assert classify_behaviour(_outcome_spec(ObservableOutcome.rendered("Some(1)"))) \
        is BehaviourClass.OPTION_SEMANTICS
    # option beats exact-format: a Display of `None` is an option promise
    assert classify_behaviour(_outcome_spec(ObservableOutcome.debug("None"))) \
        is BehaviourClass.OPTION_SEMANTICS


def test_cfg_world_needs_a_non_default_cluster_and_a_gate():
    gated = _outcome_spec(ObservableOutcome.rendered("x"),
                          given="#[cfg(error_generic_member_access)]\nfn t() {}",
                          cfg_cluster="nightly")
    assert classify_behaviour(gated, default_cluster="stable") is BehaviourClass.CFG_WORLD
    # same text on the default cluster: not another world
    assert classify_behaviour(gated, default_cluster="nightly") is BehaviourClass.OTHER
    # non-default cluster but nothing gated in the text: undetectable → OTHER
    plain = _outcome_spec(ObservableOutcome.rendered("x"), given="fn t() {}",
                          cfg_cluster="nightly")
    assert classify_behaviour(plain, default_cluster="stable") is BehaviourClass.OTHER


def test_assertion_spec_without_then_is_other():
    spec = BehaviourSpec("a", "", "let e = f();", None, cfg_cluster="stable")
    assert classify_behaviour(spec) is BehaviourClass.OTHER


def test_merge_suites_existing_wins_and_sorts():
    old = _outcome_spec(ObservableOutcome.stdout("golden"))
    old = BehaviourSpec.from_dict({**old.to_dict(), "behaviour_id": "probe::stable::f::0",
                                   "exercises": ["src/lib.rs::f"]})
    new_same = BehaviourSpec.from_dict({**old.to_dict(), "exercises": [], "then": {
        "kind": "NORMALIZED_STDOUT", "value": "other", "chain": [], "ok": False,
        "static_kind": None, "static_expr": ""}})
    new_other = BehaviourSpec.from_dict({**old.to_dict(), "behaviour_id": "a::first"})
    merged = merge_suites(BehaviourSuite("stable", (old,)),
                          BehaviourSuite("stable", (new_same, new_other)))
    assert [s.behaviour_id for s in merged.specs] == ["a::first", "probe::stable::f::0"]
    kept = merged.specs[1]
    assert kept.exercises == frozenset({"src/lib.rs::f"}) and kept.then.value == "golden"
