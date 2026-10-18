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
    coerce_identity_unit,
    drop_capture_owned,
    is_relevant,
    select_component_behaviours,
    select_identity_must,
    select_usage_examples,
)
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.models import FnRecord


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
    without a name token."""
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
    assert coerce_identity_unit(rec) == IdentityMustUnit(
        "construct_from_adhoc", "impl Error",
        "fn construct_from_adhoc<M>(error: M) -> Self")
    assert isinstance(identity_must_instruction(suite.specs, units=(rec,)), str)
    with pytest.raises(TypeError, match="IdentityMustUnit or FnRecord"):
        select_identity_must(suite.specs, units=("fmt",))  # type: ignore[arg-type]




# ---- the identity subject is the type the crate declares the method on ----

_SKELETON_RECORDS = (Path(__file__).resolve().parent / "fixtures" / "behaviours"
                     / "stable_skeleton_records_2026-10-18.json")


def test_the_recorded_suite_is_asked_about_error_and_admits_its_constructors() -> None:
    """The plan's sentence on recorded inputs: the 2026-08-27 stable suite
    and the stable skeleton's records (`enumerate_fns`, 2026-10-18). Every
    one of the 35 identity examples resolves to `Error`, and the generic
    driver admits the `impl Error` constructors. Negative control: the text
    harvest the suite was read with before names `Box` and admits none."""
    from pipeline_core.domain.behaviour_select import (
        _identity_suite,
        identity_subject_from_specs,
        resolve_identity_subjects,
    )
    suite = BehaviourSuite.from_dict(
        json.loads(_RECORDED_STABLE.read_text(encoding="utf-8")))
    records = json.loads(_SKELETON_RECORDS.read_text(encoding="utf-8"))
    drv = language_driver()

    def _admitted(subject: str) -> list[str]:
        return sorted(r["fn_name"] for r in records
                      if r["container"] == "impl Error"
                      and drv.identity_must_admits(
                          r["fn_name"], r["container"], r["sig_text"] or "", subject))

    before = identity_subject_from_specs(suite.specs)
    assert before == "Box" and _admitted(before) == []
    resolved = resolve_identity_subjects(suite.specs, records)
    ident = _identity_suite(resolved)
    assert len(ident) == 35 and {s.subject for s in ident} == {"Error"}
    after = identity_subject_from_specs(resolved)
    assert after == "Error"
    assert {"construct_from_adhoc", "construct_from_display", "construct_from_std",
            "construct_from_boxed", "construct_from_context", "msg"} <= set(
                _admitted(after))
    assert resolved == suite.specs, "the subject is not part of an example's identity"


def test_a_subject_needs_exactly_one_inherent_owner_of_the_method() -> None:
    """Invented crate: `peek` is declared on `Counter` alone (resolves),
    `probe` on two types (does not), `downcast_ref` nowhere in the crate (a
    std trait object's, does not), and a trait impl's method is not an
    owner."""
    from pipeline_core.domain.behaviour_select import (
        identity_call_name,
        resolve_identity_subjects,
    )

    def _ident(bid: str, when: str) -> BehaviourSpec:
        return BehaviourSpec(
            bid, "", when, ObservableOutcome.type_identity("u8", True),
            subkind=BehaviourSubKind.OUTCOME, klass=BehaviourClass.IDENTITY)

    specs = (_ident("a", "c.peek::<u8>()"), _ident("b", "c.probe::<u8>()"),
             _ident("c", "e.downcast_ref::<u8>()"), _ident("d", "c.fmt::<u8>()"))
    records = [
        {"fn_name": "peek", "container": "impl Counter"},
        {"fn_name": "probe", "container": "impl Counter"},
        {"fn_name": "probe", "container": "impl<T> Gauge<T>"},
        {"fn_name": "fmt", "container": "impl Display for Counter"},
    ]
    got = {s.behaviour_id: s.subject for s in resolve_identity_subjects(specs, records)}
    assert got == {"a": "Counter", "b": "", "c": "", "d": ""}
    assert identity_call_name('Counter::new(1).peek::<u8>()') == "peek"
    assert identity_call_name("c.peek()") == "peek"
    assert identity_call_name("value") == ""
