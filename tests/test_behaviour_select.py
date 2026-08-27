"""Pins for the behaviour few-shot selector (F10 Use-B / M5)."""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.behaviour_select import (
    COMPONENT_BEHAVIOUR_CAP,
    MUST_CLASS_ORDER,
    behaviour_class_name,
    is_relevant,
    select_component_behaviours,
    select_usage_examples,
)


def _spec(bid, when, *, exercises=frozenset(), fence=FenceDirective.RUN):
    return BehaviourSpec(
        behaviour_id=bid, given="", when=when, then=None,
        subkind=BehaviourSubKind.ASSERTION, fence=fence,
        exercises=exercises, source_ref=bid)


def test_relevant_by_fn_name_token():
    s = _spec("d1", 'let e = context(x); assert_eq!(e.to_string(), "y");')
    assert is_relevant(s, fn_name="context", container="")
    assert not is_relevant(s, fn_name="downcast", container="")


def test_relevant_by_container_type():
    s = _spec("d2", "let c = Chain::new(); c.next();")
    assert is_relevant(s, fn_name="next", container="impl Iterator for Chain")


def test_relevant_by_exercises_id():
    s = _spec("d3", "opaque();", exercises=frozenset({"root_cause"}))
    assert is_relevant(s, fn_name="root_cause", container="")


def test_compile_fail_never_used_as_example():
    s = _spec("d4", "bad_usage();", fence=FenceDirective.COMPILE_FAIL)
    assert not is_relevant(s, fn_name="bad_usage", container="")


def test_select_renders_matching_specs_and_respects_budget():
    specs = (
        _spec("a", 'let e = anyhow!("x"); e.chain();'),
        _spec("b", "unrelated();"),
        _spec("c", "let e2 = anyhow!(\"y\"); e2.chain();"),
    )
    out = select_usage_examples(tuple(specs), fn_name="chain", container="",
                                char_budget=10_000)
    assert "e.chain()" in out and "e2.chain()" in out
    assert "unrelated" not in out
    # whole-spec budget: a tiny budget keeps only the first, never a fragment.
    small = select_usage_examples(tuple(specs), fn_name="chain", container="",
                                  char_budget=1)
    assert small.count("chain()") <= 1


def test_select_empty_when_nothing_matches():
    specs = (_spec("a", "foo();"),)
    assert select_usage_examples(specs, fn_name="bar", container="",
                                 char_budget=1000) == ""
    assert select_usage_examples(specs, fn_name="bar", container="",
                                 char_budget=0) == ""
    matching = (_spec("a", 'let e = anyhow!("x"); e.chain();'),
                _spec("b", 'let e = anyhow!("y"); e.chain();'))
    capped = select_usage_examples(matching, fn_name="chain", container="",
                                   char_budget=10_000, max_examples=1)
    assert capped.count("chain()") == 1
    empty_code = BehaviourSpec(
        behaviour_id="e", given="", when="", then=None,
        subkind=BehaviourSubKind.ASSERTION, fence=FenceDirective.RUN,
        exercises=frozenset({"chain"}), source_ref="e")
    assert select_usage_examples(
        (empty_code,), fn_name="chain", container="", char_budget=1000) == ""


# ---------------------------------------------------------------------------
# Component selection for the layout-authoring prompt
# ---------------------------------------------------------------------------

def _outcome(bid, when, then, *, given="", exercises=frozenset(),
             fence=FenceDirective.RUN):
    return BehaviourSpec(
        behaviour_id=bid, given=given, when=when, then=then,
        subkind=BehaviourSubKind.OUTCOME, fence=fence, exercises=exercises,
        source_ref=bid)


def test_component_selection_by_exercises_and_by_name_drops_static_pins():
    specs = [
        _outcome("static", "size_of::<Error>()", ObservableOutcome.static_assertion(
            StaticAssertKind.SIZE, "size_of::<Error>() == 8")),
        _outcome("by-exercise", "opaque()", ObservableOutcome.display("x"),
                 exercises=frozenset({"src/error.rs::msg::MessageError"})),
        _outcome("by-name", "let e = MessageError(1);", ObservableOutcome.debug("y")),
        _outcome("by-public", 'Error::msg("x").is::<&str>()', ObservableOutcome.downcast_ok(True)),
        _outcome("prefix-only", "ErrorImpl::new()", ObservableOutcome.display("z")),
        _outcome("unrelated", "other()", ObservableOutcome.display("z")),
        _spec("assert-by-name", "let m = MessageError(2); assert!(m.0 == 2);"),
        _spec("assert-by-exercise-only", "opaque();", exercises=frozenset({"MessageError"})),
        _outcome("negative", "MessageError::bad()", ObservableOutcome.display("q"),
                 fence=FenceDirective.COMPILE_FAIL),
    ]
    out = select_component_behaviours(
        specs, component_names=["MessageError", "vtable"], public_types=["Error"])
    ids = [s.behaviour_id for s in out]
    assert "static" not in ids             # pins have their own section + gate
    assert "by-exercise" in ids            # `file::fn::Type` segment match
    assert "by-name" in ids                # component name in the code text
    assert "by-public" in ids              # public type whose layout reaches it
    assert "prefix-only" not in ids        # whole-word: `ErrorImpl` is not `Error`
    assert "unrelated" not in ids
    assert "assert-by-name" in ids         # assertion usage naming the component
    assert "assert-by-exercise-only" not in ids  # exercises alone never keep an assertion
    assert "negative" not in ids           # compile_fail is not a MUST
    # MUST-classes first: identity, then exact-format, unclassified usage last.
    assert ids[0] == "by-public"
    assert ids[-1] == "assert-by-name"


def test_component_selection_respects_the_cap_and_keeps_harvest_order_within_a_class():
    specs = [_outcome(f"b{i}", "let e = MessageError(1);", ObservableOutcome.display(str(i)))
             for i in range(50)]
    out = select_component_behaviours(specs, component_names=["MessageError"], public_types=[])
    assert [s.behaviour_id for s in out] == [f"b{i}" for i in range(40)]
    assert len(select_component_behaviours(
        specs, component_names=["MessageError"], public_types=[], cap=3)) == 3
    assert select_component_behaviours(
        specs, component_names=["Other"], public_types=[]) == []
    assert select_component_behaviours(
        [], component_names=["MessageError"], public_types=["Error"]) == []


def test_behaviour_class_name_prefers_a_meaningful_klass_and_sharpens_other():
    assert behaviour_class_name(_outcome("a", "x", ObservableOutcome.downcast_ok(True))) == "IDENTITY"
    assert behaviour_class_name(_outcome("b", "x", ObservableOutcome.display("d"))) == "EXACT_FORMAT"
    assert behaviour_class_name(_outcome("c", "x", ObservableOutcome.chain_seq(("a",)))) == "EXACT_FORMAT"
    assert behaviour_class_name(_spec("d", "x")) == "OTHER"
    # a spec carrying its own class (the field being added beside this change)
    klassed = SimpleNamespace(klass=SimpleNamespace(name="BOUNDED_RESOURCE"), then=None)
    assert behaviour_class_name(klassed) == "BOUNDED_RESOURCE"
    # ...but `OTHER` is not a classification: the outcome kind sharpens it.
    vague = SimpleNamespace(klass=SimpleNamespace(name="OTHER"),
                            then=ObservableOutcome.downcast_ok(False))
    assert behaviour_class_name(vague) == "IDENTITY"
    # a kind that does not exist yet still groups by its name.
    new_kind = SimpleNamespace(klass=None, then=SimpleNamespace(kind=SimpleNamespace(name="TYPE_IDENTITY")))
    assert behaviour_class_name(new_kind) == "IDENTITY"
    assert MUST_CLASS_ORDER[0] == "IDENTITY"


def test_cap_of_40_on_the_error_component_shape_drops_the_whole_bounded_resource_class():
    """Uncapped 92 for the Error/vtable component is IDENTITY 24,
    EXACT_FORMAT 51, BOUNDED_RESOURCE 4, OTHER 13. Cap 40 keeps all
    identity plus 16 exact-format and drops the entire BOUNDED_RESOURCE
    class (the 40-byte-buffer / whitespace-fallback family). Negative
    control: the uncapped match still holds all four bounded-resource
    specs."""
    identity = [_outcome(f"id-{i}", "Error::msg(x).is::<&str>()",
                         ObservableOutcome.downcast_ok(True))
                for i in range(24)]
    exact = [_outcome(f"fmt-{i}", 'format!("{}", Error::msg(x))',
                      ObservableOutcome.display(str(i)))
             for i in range(51)]
    bounded = [BehaviourSpec(
        behaviour_id=f"buf-{i}", given="", when="Error::msg(x).to_string()",
        then=ObservableOutcome.display("x" * 40),
        subkind=BehaviourSubKind.OUTCOME,
        klass=BehaviourClass.BOUNDED_RESOURCE, source_ref=f"buf-{i}")
               for i in range(4)]
    other = [_spec(f"use-{i}", "let e = Error; e.ok()") for i in range(13)]
    specs = identity + exact + bounded + other
    matched = select_component_behaviours(
        specs, component_names=["Error"], public_types=[], cap=None)
    kept = select_component_behaviours(
        specs, component_names=["Error"], public_types=[],
        cap=COMPONENT_BEHAVIOUR_CAP)
    assert len(matched) == 92
    assert Counter(behaviour_class_name(s) for s in matched) == {
        "IDENTITY": 24, "EXACT_FORMAT": 51, "BOUNDED_RESOURCE": 4, "OTHER": 13}
    assert Counter(behaviour_class_name(s) for s in kept) == {
        "IDENTITY": 24, "EXACT_FORMAT": 16}
    assert "BOUNDED_RESOURCE" not in {behaviour_class_name(s) for s in kept}
