"""Pins for the behaviour few-shot selector."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core.application.author_layout import _dropped_in_class
from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    FenceDirective,
    ObservableOutcome,
    StaticAssertKind,
    classified,
)
from pipeline_core.domain.behaviour_pinning import pinnable_specs
from pipeline_core.domain.behaviour_select import (
    COMPONENT_BEHAVIOUR_CAP,
    IDENTITY_CLASS,
    MUST_CLASS_ORDER,
    IdentityMustUnit,
    behaviour_class_name,
    capture_owned,
    catalog_identity_must_admits,
    drop_capture_owned,
    identity_must_admits,
    is_relevant,
    select_component_behaviours,
    select_identity_must,
    select_usage_examples,
)
from pipeline_core.domain.language_driver import language_driver, using_driver
from pipeline_core.domain.models import FnRecord
from pipeline_core.prompts.language_catalog import catalog_driver


def _spec(bid, when, *, exercises=frozenset(), fence=FenceDirective.RUN):
    return BehaviourSpec(
        behaviour_id=bid,
        setup="",
        when=when,
        then=None,
        subkind=BehaviourSubKind.ASSERTION,
        fence=fence,
        exercises=exercises,
        source_ref=bid,
    )


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
        _spec("c", 'let e2 = anyhow!("y"); e2.chain();'),
    )
    out = select_usage_examples(tuple(specs), fn_name="chain", container="", char_budget=10_000)
    assert "e.chain()" in out and "e2.chain()" in out
    assert "unrelated" not in out
    # whole-spec budget: a tiny budget keeps only the first, never a fragment.
    small = select_usage_examples(tuple(specs), fn_name="chain", container="", char_budget=1)
    assert small.count("chain()") <= 1


def test_select_empty_when_nothing_matches():
    specs = (_spec("a", "foo();"),)
    assert select_usage_examples(specs, fn_name="bar", container="", char_budget=1000) == ""
    assert select_usage_examples(specs, fn_name="bar", container="", char_budget=0) == ""
    matching = (_spec("a", 'let e = anyhow!("x"); e.chain();'), _spec("b", 'let e = anyhow!("y"); e.chain();'))
    capped = select_usage_examples(matching, fn_name="chain", container="", char_budget=10_000, max_examples=1)
    assert capped.count("chain()") == 1
    empty_code = BehaviourSpec(
        behaviour_id="e",
        setup="",
        when="",
        then=None,
        subkind=BehaviourSubKind.ASSERTION,
        fence=FenceDirective.RUN,
        exercises=frozenset({"chain"}),
        source_ref="e",
    )
    assert select_usage_examples((empty_code,), fn_name="chain", container="", char_budget=1000) == ""


# ---------------------------------------------------------------------------
# Component selection for the layout-authoring prompt
# ---------------------------------------------------------------------------


def _outcome(bid, when, then, *, setup="", exercises=frozenset(), fence=FenceDirective.RUN):
    return BehaviourSpec(
        behaviour_id=bid,
        setup=setup,
        when=when,
        then=then,
        subkind=BehaviourSubKind.OUTCOME,
        fence=fence,
        exercises=exercises,
        source_ref=bid,
    )


def test_component_selection_by_exercises_and_by_name_drops_static_pins():
    specs = [
        _outcome(
            "static",
            "size_of::<Error>()",
            ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<Error>() == 8"),
        ),
        _outcome(
            "by-exercise",
            "opaque()",
            ObservableOutcome.display("x"),
            exercises=frozenset({"src/error.rs::msg::MessageError"}),
        ),
        _outcome("by-name", "let e = MessageError(1);", ObservableOutcome.debug("y")),
        _outcome("by-public", 'Error::msg("x").is::<&str>()', ObservableOutcome.downcast_ok(True)),
        _outcome("prefix-only", "ErrorImpl::new()", ObservableOutcome.display("z")),
        _outcome("unrelated", "other()", ObservableOutcome.display("z")),
        _spec("assert-by-name", "let m = MessageError(2); assert!(m.0 == 2);"),
        _spec("assert-by-exercise-only", "opaque();", exercises=frozenset({"MessageError"})),
        _outcome("negative", "MessageError::bad()", ObservableOutcome.display("q"), fence=FenceDirective.COMPILE_FAIL),
    ]
    out = select_component_behaviours(specs, component_names=["MessageError", "vtable"], public_types=["Error"])
    ids = [s.behaviour_id for s in out]
    assert "static" not in ids  # pins have their own section + gate
    assert "by-exercise" in ids  # `file::fn::Type` segment match
    assert "by-name" in ids  # component name in the code text
    assert "by-public" in ids  # public type whose layout reaches it
    assert "prefix-only" not in ids  # whole-word: `ErrorImpl` is not `Error`
    assert "unrelated" not in ids
    assert "assert-by-name" in ids  # assertion usage naming the component
    assert "assert-by-exercise-only" not in ids  # exercises alone never keep an assertion
    assert "negative" not in ids  # compile_fail is not a MUST
    # MUST-classes first: identity, then exact-format, unclassified usage last.
    assert ids[0] == "by-public"
    assert ids[-1] == "assert-by-name"


def test_component_selection_respects_the_cap_and_keeps_harvest_order_within_a_class():
    specs = [_outcome(f"b{i}", "let e = MessageError(1);", ObservableOutcome.display(str(i))) for i in range(50)]
    out = select_component_behaviours(specs, component_names=["MessageError"], public_types=[])
    assert [s.behaviour_id for s in out] == [f"b{i}" for i in range(40)]
    assert len(select_component_behaviours(specs, component_names=["MessageError"], public_types=[], cap=3)) == 3
    assert select_component_behaviours(specs, component_names=["Other"], public_types=[]) == []
    assert select_component_behaviours([], component_names=["MessageError"], public_types=["Error"]) == []


def test_behaviour_class_name_prefers_a_meaningful_klass_and_sharpens_other():
    assert behaviour_class_name(_outcome("a", "x", ObservableOutcome.downcast_ok(True))) == "IDENTITY"
    assert behaviour_class_name(_outcome("b", "x", ObservableOutcome.display("d"))) == "EXACT_FORMAT"
    assert behaviour_class_name(_outcome("c", "x", ObservableOutcome.chain_seq(("a",)))) == "EXACT_FORMAT"
    assert behaviour_class_name(_spec("d", "x")) == "OTHER"
    # a spec carrying its own class (the field being added beside this change)
    klassed = SimpleNamespace(klass=SimpleNamespace(name="BOUNDED_RESOURCE"), then=None)
    assert behaviour_class_name(klassed) == "BOUNDED_RESOURCE"
    # ...but `OTHER` is not a classification: the outcome kind sharpens it.
    vague = SimpleNamespace(klass=SimpleNamespace(name="OTHER"), then=ObservableOutcome.downcast_ok(False))
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
    identity = [
        _outcome(f"id-{i}", "Error::msg(x).is::<&str>()", ObservableOutcome.downcast_ok(True)) for i in range(24)
    ]
    exact = [_outcome(f"fmt-{i}", 'format!("{}", Error::msg(x))', ObservableOutcome.display(str(i))) for i in range(51)]
    bounded = [
        BehaviourSpec(
            behaviour_id=f"buf-{i}",
            setup="",
            when="Error::msg(x).to_string()",
            then=ObservableOutcome.display("x" * 40),
            subkind=BehaviourSubKind.OUTCOME,
            klass=BehaviourClass.BOUNDED_RESOURCE,
            source_ref=f"buf-{i}",
        )
        for i in range(4)
    ]
    other = [_spec(f"use-{i}", "let e = Error; e.ok()") for i in range(13)]
    specs = identity + exact + bounded + other
    matched = select_component_behaviours(specs, component_names=["Error"], public_types=[], cap=None)
    kept = select_component_behaviours(specs, component_names=["Error"], public_types=[], cap=COMPONENT_BEHAVIOUR_CAP)
    assert len(matched) == 92
    assert Counter(behaviour_class_name(s) for s in matched) == {
        "IDENTITY": 24,
        "EXACT_FORMAT": 51,
        "BOUNDED_RESOURCE": 4,
        "OTHER": 13,
    }
    assert Counter(behaviour_class_name(s) for s in kept) == {"IDENTITY": 24, "EXACT_FORMAT": 16}
    assert "BOUNDED_RESOURCE" not in {behaviour_class_name(s) for s in kept}
    # The cap truncates the TAIL. That is a design choice only while IDENTITY
    # survives it: a wrap-the-payload-and-key-the-wrapper representation
    # satisfies every layout pin and every format string, and an identity Then
    # is the only class that refutes it. Asserted on THIS fixture — the one
    # that goes through `select_component_behaviours` on the live component
    # shape — because a hand-sliced list sorted by `class_rank` passes by
    # construction and would fail on nothing.
    assert _dropped_in_class(matched, kept, IDENTITY_CLASS) == 0
    assert _dropped_in_class(matched, kept, "EXACT_FORMAT") == 35
    assert _dropped_in_class(matched, kept, "BOUNDED_RESOURCE") == 4


def test_identity_beyond_the_cap_is_reported_as_dropped() -> None:
    """The case the warning exists for: MORE identity behaviours than the cap.
    Nothing on the live tree produces this shape (the Error/vtable component
    is 24 of 40), so it is asserted here rather than discovered in a run — if
    a component ever does, the operator must raise COMPONENT_BEHAVIOUR_CAP
    instead of shipping a prompt whose refuting class was silently trimmed.
    Driven through `select_component_behaviours`, not a hand slice."""
    specs = [
        _outcome(f"id-{i}", f"Error::msg(x).is::<T{i}>()", ObservableOutcome.downcast_ok(True))
        for i in range(COMPONENT_BEHAVIOUR_CAP + 7)
    ]
    matched = select_component_behaviours(specs, component_names=["Error"], public_types=[], cap=None)
    kept = select_component_behaviours(specs, component_names=["Error"], public_types=[], cap=COMPONENT_BEHAVIOUR_CAP)
    assert len(matched) == COMPONENT_BEHAVIOUR_CAP + 7
    assert _dropped_in_class(matched, kept, IDENTITY_CLASS) == 7


# --------------------------------------------------------------------------
# The capture/guess split: which classes task0c stops listing (and stops
# requiring a pin on) because their exact value can only be captured, not
# predicted from the source.
# --------------------------------------------------------------------------


def test_capture_owned_is_everything_the_guess_route_cannot_answer():
    for name in ("EXACT_FORMAT", "IDENTITY", "TRAIT_OBJECT_IDENTITY", "BOUNDED_RESOURCE"):
        spec = _outcome(name, "e.to_string()", ObservableOutcome.display("x"))
        assert capture_owned(replace(spec, klass=BehaviourClass[name])), name
    for name in ("OPTION_SEMANTICS", "LAYOUT_PIN", "CFG_WORLD"):
        spec = _outcome(name, "e.to_string()", ObservableOutcome.display("x"))
        assert not capture_owned(replace(spec, klass=BehaviourClass[name])), name
    # An assertion-only spec carries no class and no `then`: OTHER, and the
    # guess route keeps it — its asserts already say what to check.
    assert not capture_owned(_spec("assert-only", "opaque();"))


def test_an_unclassified_display_spec_is_capture_owned_by_its_outcome_kind():
    """The live harvest shape: `klass` still OTHER, the Then a Display string.
    `behaviour_class_name` sharpens it to EXACT_FORMAT, so the split must see
    it as capture-owned — otherwise the filter is a no-op on the whole suite."""
    raw = _outcome("display", "e.to_string()", ObservableOutcome.display("division by zero"))
    assert raw.klass is BehaviourClass.OTHER
    assert behaviour_class_name(raw) == "EXACT_FORMAT"
    assert capture_owned(raw)


def test_drop_capture_owned_keeps_only_the_guess_route_classes_in_harvest_order():
    exact = _outcome("exact", "e.to_string()", ObservableOutcome.display("division by zero"))
    option = classified(
        _outcome("option", 'format!("{:?}", e.downcast_ref::<E>())', ObservableOutcome.display("Some(DivByZero)"))
    )
    identity = _outcome("identity", "e.is::<E>()", ObservableOutcome.type_identity("E", True))
    assert option.klass is BehaviourClass.OPTION_SEMANTICS
    kept = drop_capture_owned((exact, option, identity))
    assert [s.behaviour_id for s in kept] == ["option"]


def test_drop_capture_owned_disabled_drops_nothing():
    """The knob's off position — the pre-split listing, verbatim."""
    specs = (
        _outcome("exact", "e.to_string()", ObservableOutcome.display("division by zero")),
        _outcome("identity", "e.is::<E>()", ObservableOutcome.type_identity("E", True)),
    )
    assert drop_capture_owned(specs, enabled=False) == specs
    assert drop_capture_owned(specs) == ()


def test_a_kind_the_classifier_cannot_name_is_still_capture_owned() -> None:
    """OTHER is not a licence. `behaviour_class_name` reads a kind it does not
    know as OTHER, and it knows neither RETURN_RENDERED nor NORMALIZED_STDOUT
    — the two kinds whose Then is most obviously an exact value a model would
    have to predict. A class allowlist left them on the guess route. The guess
    route is now the allowlist, so they are capture-owned without a kinds
    list that has to grow every time a new exact-value kind appears.

    Computed on the live 230-spec suite (2026-08-27): of 155 pinnable specs
    the class-only filter left 6 — three free-form probe blobs and three
    RETURN_RENDERED values out of `test_downcast_mut`."""
    blob = _outcome("blob", "Error::backtrace", ObservableOutcome.stdout("backtrace debug:\n<disabled>"))
    ret = _outcome(
        "ret", "err.downcast_ref::<HighLevel>().unwrap().message", ObservableOutcome.rendered("high context")
    )
    for spec in (blob, ret):
        # NEGATIVE CONTROL: class-only keeps both — they are OTHER, which is
        # the classifier's junk drawer, not a licence.
        assert behaviour_class_name(spec) == "OTHER"
        assert spec.klass is BehaviourClass.OTHER
        assert capture_owned(spec), spec.behaviour_id
    assert drop_capture_owned((blob, ret)) == ()


def test_the_guess_route_still_keeps_what_the_source_states() -> None:
    """The split is by what must be PREDICTED, not a blanket off switch: a
    layout pin and an option shape are both derivable from the source the
    prompt already carries."""
    layout = _outcome(
        "layout",
        "size_of::<Error>()",
        ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<Error>() == 8"),
    )
    option = classified(_outcome("option", "e.source().is_none()", ObservableOutcome.display("true")))
    assert option.klass is BehaviourClass.OPTION_SEMANTICS
    assert not capture_owned(layout) and not capture_owned(option)
    assert drop_capture_owned((layout, option)) == (layout, option)


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
_CLASS_ONLY = frozenset(
    {
        "EXACT_FORMAT",
        "IDENTITY",
        "TRAIT_OBJECT_IDENTITY",
        "BOUNDED_RESOURCE",
    }
)


def test_the_recorded_suite_leaves_no_pinnable_spec_on_the_guess_route() -> None:
    """The plan's fixture, computed on the recorded `_behaviours_stable.json` (2026-08-27):
    230 specs, 155 pinnable; the class-only filter left 6; the inverted
    allowlist leaves 0. Negative control is the class-only predicate on the
    same records — a hand-made list of those six ids would pass by
    construction and fail on nothing."""
    suite = BehaviourSuite.from_dict(json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))
    assert len(suite.specs) == 230
    pinnable = pinnable_specs(suite.specs)
    assert len(pinnable) == 155
    class_only_left = tuple(s for s in pinnable if behaviour_class_name(s) not in _CLASS_ONLY)
    assert len(class_only_left) == 6
    assert {behaviour_class_name(s) for s in class_only_left} == {"OTHER"}
    kinds = {s.then.kind.name for s in class_only_left if s.then is not None}
    assert kinds == {"NORMALIZED_STDOUT", "RETURN_RENDERED"}
    assert drop_capture_owned(pinnable) == ()


# Constructor names a file-first `impl Error` write: list actually carries.
# The plan's fixture: on this recorded suite the first three unique identity
# Thens for that batch are `&str` / `String` / `io::Error` — harvest order
# on the same batch starts at `Box<dyn …>` / `HighLevel` and never reaches
# `test_downcast`.
_CONSTRUCTOR_BATCH = (
    "construct", "msg", "new",
    "construct_from_adhoc", "construct_from_std",
    "construct_from_display", "construct_from_boxed",
    "construct_from_context", "context",
)


def test_identity_must_on_recorded_constructor_batch_is_str_string_ioerror() -> None:
    """Plan sentence: on the recorded 230-spec suite, the first three unique
    identity Thens for the `impl Error` constructor batch are `&str`,
    `String`, `io::Error`. Negative control is `select_usage_examples` for
    `construct` in `impl Error` on the same records — that blob starts at
    `doctest::error.rs::311` with `unimplemented!()` and contains neither
    `is::<` nor 'never a wrapper'."""
    suite = BehaviourSuite.from_dict(json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))
    picked = select_identity_must(suite.specs, fn_names=_CONSTRUCTOR_BATCH)
    assert [s.then.value for s in picked if s.then is not None] == [
        "&str", "String", "io::Error"]
    assert all(s.then.ok for s in picked if s.then is not None)
    adhoc = select_identity_must(suite.specs, fn_names=("construct_from_adhoc",))
    assert [s.then.value for s in adhoc if s.then is not None] == [
        "&str", "String", "io::Error"]
    assert select_identity_must(suite.specs, fn_names=("chain",)) == []
    ue = select_usage_examples(
        tuple(suite.specs), fn_name="construct", container="impl Error",
        char_budget=1200)
    assert "unimplemented!" in ue
    assert "is::<" not in ue
    assert "never a wrapper" not in ue
    assert "doctest::error.rs::311" in ue or "error.rs:311" in ue


def test_identity_must_does_not_match_container_or_display_or_given_fmt() -> None:
    """A DISPLAY spec that names the constructor, an OTHER doctest whose
    text names `Error`, and a TYPE_IDENTITY whose `given` contains `fn fmt`
    must not steal the cap from the spec whose leaf exercise is the unit."""
    pin = _outcome(
        "pin", "error.downcast::<&str>()",
        ObservableOutcome.type_identity("&str", True),
        exercises=frozenset({"src/error.rs::construct_from_adhoc"}),
    )
    display = _outcome(
        "disp", 'error.to_string()',
        ObservableOutcome.display("x"),
        exercises=frozenset({"construct_from_adhoc"}),
    )
    doctest = _spec("doc", 'let e = Error::msg("x"); unimplemented!()')
    fmt_given = _outcome(
        "fmt-given", "err.is::<MyErr>()",
        ObservableOutcome.type_identity("MyErr", True),
        setup="use std::fmt::{self, Display}; impl Display for MyErr { fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { write!(f, \"e\") } }",
        exercises=frozenset({"src/error.rs::new"}),
    )
    picked = select_identity_must(
        (pin, display, doctest, fmt_given),
        fn_names=("construct_from_adhoc",))
    assert [s.behaviour_id for s in picked] == ["pin"]
    assert select_identity_must((pin, fmt_given), fn_names=("fmt",)) == []


def test_identity_must_ranks_non_flood_hits_ahead_of_the_construct_dump() -> None:
    """Five TYPE_IDENTITY specs that only name `construct` plus one that
    names `msg` — `construct` floods, so a batch of `{construct, msg}`
    must lead with the `msg` spec's Then, not harvest-order `A`."""
    dump = tuple(
        _outcome(
            f"dump{i}", "opaque()",
            ObservableOutcome.type_identity(f"T{i}", True),
            exercises=frozenset({"src/error.rs::construct"}),
        )
        for i in range(5)
    )
    pin = _outcome(
        "msg-pin", "error.downcast::<&str>()",
        ObservableOutcome.type_identity("&str", True),
        exercises=frozenset({"src/error.rs::msg", "src/error.rs::construct"}),
    )
    picked = select_identity_must(
        (*dump, pin), fn_names=("construct", "msg"))
    assert picked[0].behaviour_id == "msg-pin"
    assert picked[0].then is not None and picked[0].then.value == "&str"


def test_identity_must_admits_is_stub_time_role_not_bare_self() -> None:
    """Admission on -> Self whose self-type equals the type the suite's
    identity when names. A Counter suite admits Counter::new and refuses
    impl Display for Counter. TypeId-plus-Option remains a downcast slot
    without a name token. Catalog still pins Error constructors."""
    drv = language_driver()
    assert drv.name == "generic"
    assert drv.identity_must_admits(
        "new", "impl Counter", "fn new() -> Self", "Counter")
    assert not drv.identity_must_admits(
        "fmt", "impl Display for Counter",
        "fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result", "Counter")
    assert not drv.identity_must_admits(
        "new", "impl Own < T >", "fn new(ptr: NonNull<T>) -> Self", "Counter")
    assert not drv.identity_must_admits(
        "new", "impl Buf", "fn new() -> Self", "Counter")
    assert drv.identity_must_admits(
        "slot", "impl Node",
        "fn slot<E>(e: Ref<Node>, target: TypeId) -> Option<Ref<()>>",
        "Counter")
    assert not drv.identity_must_admits(
        "drop_rest", "",
        "fn drop_rest<E>(e: Own<Node>, target: TypeId)", "Counter")
    # Catalog A/B: Error constructors and the downcast name token.
    assert catalog_identity_must_admits(
        "construct_from_display", "impl Error",
        "fn construct_from_display<M>(error: M, backtrace: Backtrace) -> Self")
    assert catalog_identity_must_admits(
        "from", "impl From < E > for Error", "fn from(error: E) -> Self")
    assert not catalog_identity_must_admits(
        "from",
        "impl From < Error > for Box < dyn StdError + Send + Sync + 'static >",
        "fn from(error: Error) -> Self")
    assert not catalog_identity_must_admits(
        "object_drop_front", "",
        "fn object_drop_front<E>(e: Own<ErrorImpl>, target: TypeId)")
    assert catalog_identity_must_admits(
        "object_downcast", "",
        "fn object_downcast<E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>")


_FRONTIER_STUBS = (
    Path(__file__).resolve().parent / "fixtures" / "behaviours"
    / "identity_must_frontier_stubs_2026-08-28.json"
)


def test_identity_must_aiming_on_recorded_frontier_drops_wrapper_fmt() -> None:
    """Plan sentence, computed on the frozen 85-stub frontier and the
    recorded 230-spec suite: names-only still hands a MUST to 53/85 units
    including all six wrapper.rs fmt bodies (the negative control); aimed
    is 16/85 with those six empty; every construct_from_* keeps a MUST
    (adhoc/display Then `&str`, including solo construct_from_display via
    sibling leaves); Own/Buf/Ref `new` are not admitted. A hand-made list
    of constructor names would pass by construction — the fixture is the
    live stub records.
    """
    with using_driver(catalog_driver()):
        _identity_must_aiming_on_recorded_frontier()


def _identity_must_aiming_on_recorded_frontier() -> None:
    from pipeline_core.prompts.layout_authoring import identity_must_instruction

    stubs = json.loads(_FRONTIER_STUBS.read_text(encoding="utf-8"))
    assert len(stubs) == 85
    suite = BehaviourSuite.from_dict(
        json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))

    names_only = [
        s for s in stubs
        if identity_must_instruction(suite.specs, fn_names=(s["fn_name"],))]
    assert len(names_only) == 53
    wrapper_fmt = [
        s for s in stubs
        if s["file"].endswith("wrapper.rs") and s["fn_name"] == "fmt"]
    assert len(wrapper_fmt) == 6
    assert sum(
        1 for s in names_only
        if s["file"].endswith("wrapper.rs") and s["fn_name"] == "fmt") == 6

    def _unit(s: dict) -> IdentityMustUnit:
        return IdentityMustUnit(s["fn_name"], s["container"], s["sig_text"])

    aimed = [
        s for s in stubs
        if identity_must_instruction(suite.specs, units=(_unit(s),))]
    assert len(aimed) == 16
    assert not any(
        s["file"].endswith("wrapper.rs") and s["fn_name"] == "fmt"
        for s in aimed)
    assert not any("ptr.rs" in s["file"] for s in aimed)

    for s in wrapper_fmt:
        assert not identity_must_admits(s["fn_name"], s["container"], s["sig_text"])
        assert identity_must_instruction(suite.specs, units=(_unit(s),)) == ""

    construct_from = [
        s for s in stubs if s["fn_name"].startswith("construct_from_")]
    assert {s["fn_name"] for s in construct_from} == {
        "construct_from_adhoc", "construct_from_std", "construct_from_display",
        "construct_from_boxed", "construct_from_context"}
    for s in construct_from:
        text = identity_must_instruction(suite.specs, units=(_unit(s),))
        assert text.startswith("MUST:"), s["fn_name"]
        assert "never a wrapper's" in text
    # The wrap the 2026-08-26 fills missed is `&str`. construct_from_context
    # / construct_from_boxed have their own identity Thens (HighLevel /
    # Box<dyn>); the plan's sentence is that they KEEP an instruction, not
    # that every constructor's solo Then is `&str`.
    for s in construct_from:
        if s["fn_name"] not in (
                "construct_from_adhoc", "construct_from_display"):
            continue
        text = identity_must_instruction(suite.specs, units=(_unit(s),))
        assert "`is::<&str>()`" in text, s["fn_name"]

    display = next(s for s in construct_from
                   if s["fn_name"] == "construct_from_display")
    assert select_identity_must(
        suite.specs, fn_names=("construct_from_display",)) == []
    picked = select_identity_must(suite.specs, units=(_unit(display),))
    assert [s.then.value for s in picked if s.then is not None] == [
        "&str", "String", "io::Error"]

    error_ctors = [
        s for s in stubs
        if s["container"] == "impl Error"
        and identity_must_admits(s["fn_name"], s["container"], s["sig_text"])]
    block = identity_must_instruction(
        suite.specs, units=tuple(_unit(s) for s in error_ctors))
    assert "`is::<&str>()`" in block
    assert "never a wrapper's" in block

    for s in stubs:
        if s["fn_name"] != "new":
            continue
        assert not identity_must_admits(
            s["fn_name"], s["container"], s["sig_text"]), s["container"]


def test_select_identity_must_units_accept_fn_record_and_reject_a_bare_name() -> None:
    """Production passes FnRecord; a bare str used to coerce to a name-only
    unit and fail closed silently. The argument is IdentityMustInput now."""
    from pipeline_core.prompts.layout_authoring import identity_must_instruction

    suite = BehaviourSuite.from_dict(
        json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))
    rec = FnRecord(
        fn_name="construct_from_adhoc",
        container="impl Error",
        sig_text="fn construct_from_adhoc<M>(error: M) -> Self")
    with using_driver(catalog_driver()):
        text = identity_must_instruction(suite.specs, units=(rec,))
    assert text.startswith("MUST:")
    assert "`is::<&str>()`" in text
    with pytest.raises(TypeError, match="IdentityMustUnit or FnRecord"):
        select_identity_must(suite.specs, units=("fmt",))  # type: ignore[arg-type]


