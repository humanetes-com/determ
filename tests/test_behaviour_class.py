"""Pins for the behaviour vocabulary added 2026-08-26: TYPE_IDENTITY /
ALT_DEBUG_STRING outcomes, BehaviourClass + classify_behaviour, the MAY note,
and merge_suites (existing wins)."""

from __future__ import annotations

import json
from pathlib import Path

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


def _outcome_spec(
    then: ObservableOutcome, *, when: str = "x", setup: str = "", cfg_cluster: str = "stable"
) -> BehaviourSpec:
    return BehaviourSpec("b", setup, when, then, subkind=BehaviourSubKind.OUTCOME, cfg_cluster=cfg_cluster)


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
        "The layout, allocation strategy and private names are yours (MAY); every behaviour listed is a MUST."
    )


def test_klass_serialises_by_name_and_defaults_other_on_old_records():
    spec = classified(_outcome_spec(ObservableOutcome.type_identity("&str", True)))
    d = spec.to_dict()
    assert d["klass"] == "IDENTITY"
    assert BehaviourSpec.from_dict(d) == spec
    d.pop("klass")  # a record written before the field
    assert BehaviourSpec.from_dict(d).klass is BehaviourClass.OTHER


def test_classify_precedence_table():
    static = ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<E>()")
    assert classify_behaviour(_outcome_spec(static)) is BehaviourClass.LAYOUT_PIN
    ident = ObservableOutcome.type_identity("&str", True)
    assert classify_behaviour(_outcome_spec(ident, when="e.downcast::<&str>()")) is BehaviourClass.IDENTITY
    assert (
        classify_behaviour(_outcome_spec(ident, when="(&*e as &dyn Any).type_id()"))
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    # The wrapping defect is the cast, not the substring `type_id()`. A capture
    # of `(&*err as &dyn Error).is::<&str>()` never writes `type_id()`, and
    # keying on that substring made TRAIT_OBJECT_IDENTITY unstorable.
    assert (
        classify_behaviour(_outcome_spec(ident, when="(&*err as &dyn Error).is::<&str>()"))
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    assert (
        classify_behaviour(_outcome_spec(ident, when="(&*err as &mut dyn Error).is::<&str>()"))
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    assert (
        classify_behaviour(_outcome_spec(ident, when="x as &'static dyn Error")) is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    assert (
        classify_behaviour(_outcome_spec(ident, when="x as &(dyn Error + Send)"))
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    # Rust writes the lifetime BEFORE `mut`, and `&'a mut dyn` is the only
    # legal spelling when both appear. Reading `mut` first accepted the
    # unparseable `&mut 'a dyn` and rejected this one.
    assert (
        classify_behaviour(_outcome_spec(ident, when="x as &'a mut dyn Error"))
        is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    # A raw pointer to a trait object answers through the same vtable.
    assert (
        classify_behaviour(_outcome_spec(ident, when="e as *const dyn Error")) is BehaviourClass.TRAIT_OBJECT_IDENTITY
    )
    assert classify_behaviour(_outcome_spec(ident, when="e as *mut dyn Error")) is BehaviourClass.TRAIT_OBJECT_IDENTITY
    # NEGATIVE CONTROL: `&mut 'a dyn` does not parse as Rust, so accepting it
    # would only ever match text no compiler produced.
    assert classify_behaviour(_outcome_spec(ident, when="x as &mut 'a dyn Error")) is BehaviourClass.IDENTITY
    # NEGATIVE CONTROL: an ordinary numeric cast, and a `dyn`-prefixed ident.
    assert classify_behaviour(_outcome_spec(ident, when="len as usize")) is BehaviourClass.IDENTITY
    assert classify_behaviour(_outcome_spec(ident, when="x as &dynamic")) is BehaviourClass.IDENTITY
    # NEGATIVE CONTROL: `dyn` inside a turbofish type argument is IDENTITY —
    # this is the live harvested `when` of test_boxed_str::2.
    assert (
        classify_behaviour(_outcome_spec(ident, when="error.downcast_ref::<Box<dyn StdError + Send + Sync>>()"))
        is BehaviourClass.IDENTITY
    )
    # NEGATIVE CONTROL: the old marker, without a trait-object cast.
    assert classify_behaviour(_outcome_spec(ident, when="e.type_id()")) is BehaviourClass.IDENTITY
    assert classify_behaviour(_outcome_spec(ObservableOutcome.display("boom"))) is BehaviourClass.EXACT_FORMAT
    assert classify_behaviour(_outcome_spec(ObservableOutcome.alt_debug("X {\n}"))) is BehaviourClass.EXACT_FORMAT
    bounded = _outcome_spec(ObservableOutcome.display("aaa"), setup='let s = "a".repeat(3);\nassert_eq!(s.len(), 3);')
    assert classify_behaviour(bounded) is BehaviourClass.BOUNDED_RESOURCE
    commented = _outcome_spec(
        ObservableOutcome.display("x"), setup="// the message is capped at the limit\nlet s = f();"
    )
    assert classify_behaviour(commented) is BehaviourClass.BOUNDED_RESOURCE
    assert (
        classify_behaviour(_outcome_spec(ObservableOutcome.rendered("x"), when="e.source().is_none()"))
        is BehaviourClass.OPTION_SEMANTICS
    )
    assert classify_behaviour(_outcome_spec(ObservableOutcome.rendered("Some(1)"))) is BehaviourClass.OPTION_SEMANTICS
    # option beats exact-format: a Display of `None` is an option promise
    assert classify_behaviour(_outcome_spec(ObservableOutcome.debug("None"))) is BehaviourClass.OPTION_SEMANTICS


def test_cfg_world_needs_a_non_default_cluster_and_a_gate():
    gated = _outcome_spec(
        ObservableOutcome.rendered("x"), setup="#[cfg(error_generic_member_access)]\nfn t() {}", cfg_cluster="nightly"
    )
    assert classify_behaviour(gated, default_cluster="stable") is BehaviourClass.CFG_WORLD
    # same text on the default cluster: not another world
    assert classify_behaviour(gated, default_cluster="nightly") is BehaviourClass.OTHER
    # non-default cluster but nothing gated in the text: undetectable → OTHER
    plain = _outcome_spec(ObservableOutcome.rendered("x"), setup="fn t() {}", cfg_cluster="nightly")
    assert classify_behaviour(plain, default_cluster="stable") is BehaviourClass.OTHER


def test_assertion_spec_without_then_is_other():
    spec = BehaviourSpec("a", "", "let e = f();", None, cfg_cluster="stable")
    assert classify_behaviour(spec) is BehaviourClass.OTHER


def test_merge_suites_existing_wins_and_sorts():
    old = _outcome_spec(ObservableOutcome.stdout("golden"))
    old = BehaviourSpec.from_dict(
        {**old.to_dict(), "behaviour_id": "probe::stable::f::0", "exercises": ["src/lib.rs::f"]}
    )
    new_same = BehaviourSpec.from_dict(
        {
            **old.to_dict(),
            "exercises": [],
            "then": {
                "kind": "NORMALIZED_STDOUT",
                "value": "other",
                "chain": [],
                "ok": False,
                "static_kind": None,
                "static_expr": "",
            },
        }
    )
    new_other = BehaviourSpec.from_dict({**old.to_dict(), "behaviour_id": "a::first"})
    merged = merge_suites(BehaviourSuite("stable", (old,)), BehaviourSuite("stable", (new_same, new_other)))
    assert [s.behaviour_id for s in merged.specs] == ["a::first", "probe::stable::f::0"]
    kept = merged.specs[1]
    assert kept.exercises == frozenset({"src/lib.rs::f"}) and kept.then.value == "golden"


def test_outcome_without_then_and_assertion_with_then_are_rejected():
    with pytest.raises(ValueError, match="OUTCOME behaviour requires"):
        BehaviourSpec("b", "", "x", None, subkind=BehaviourSubKind.OUTCOME)
    with pytest.raises(ValueError, match="ASSERTION behaviour"):
        BehaviourSpec("b", "", "x", ObservableOutcome.display("v"), subkind=BehaviourSubKind.ASSERTION)


# The bytes t0d wrote to `tmp/anyhow/.skeleton/_behaviours_stable.json` on
# 2026-08-27, copied here verbatim. Reading the live path instead couples this
# gate to an artefact the pipeline REWRITES: `save_behaviour_suite` rewrites it
# on every t0d pass (which is why `oracle_fingerprint` keys on it), `tmp` is
# gitignored so a fresh clone has no file at all, and `redo.sh` removes it. Any
# of the three turns a green check.sh red, and a red check.sh means pre_run.sh
# writes no stamp and continue.sh exits 3 — a launch blocked by a test about
# records from a previous run. Frozen here, the counts below stay a claim about
# a measurement instead of a claim about the current workspace.
_RECORDED_STABLE = Path(__file__).resolve().parent / "fixtures" / "behaviours" / "behaviours_stable_2026-08-27.json"


def test_the_recorded_suite_has_no_trait_object_identity_and_the_box_dyn_stays_identity():
    """Computed on the recorded `_behaviours_stable.json` (2026-08-27): 230 specs, 35
    TYPE_IDENTITY, 0 TRAIT_OBJECT_IDENTITY. The one TYPE_IDENTITY whose `when`
    contains `dyn` is `test_boxed_str::2` — a Box<dyn> *type argument*, not a
    trait-object observation. A classifier that keyed on the letters `dyn`
    would flip it; this one must not. Histogram does not move; new captures
    of `(&*err as &dyn Error).is::<&str>()` would store as TRAIT_OBJECT."""
    suite = BehaviourSuite.from_dict(json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))
    assert len(suite.specs) == 230
    assert sum(1 for s in suite.specs if s.klass is BehaviourClass.TRAIT_OBJECT_IDENTITY) == 0
    assert sum(1 for s in suite.specs if classify_behaviour(s) is BehaviourClass.TRAIT_OBJECT_IDENTITY) == 0
    boxed = next(s for s in suite.specs if s.behaviour_id.endswith("test_boxed_str::2"))
    assert boxed.when == "error.downcast_ref::<Box<dyn StdError + Send + Sync>>()"
    assert boxed.klass is BehaviourClass.IDENTITY
    assert classify_behaviour(boxed) is BehaviourClass.IDENTITY
