"""An E0277 retry names the crate type that holds the rejected value.

Run 371055-1a407b3c: ten replies passed an unsized trait object by value.
The reply that compiled wrapped it in the one crate struct whose field is
a Box of that object and which implements the trait. That struct's name is
not in the function signature, so types_referenced_in_text does not return
it. The diagnostic does, and the records do.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.blamed_type_api import (
    adapter_types_for_diagnostic,
    with_blamed_type_api,
)
from pipeline_core.domain.models import FieldRecord, TraitImplRecord, TypeRecord
from pipeline_core.domain.type_refs import types_referenced_in_text

# The shape of the file retry on that run: the compiler's dyn path and the
# source bound's name for the same trait, in one error.
_E0277 = """\
error[E0277]: the size for values of type `dyn core::error::Error + Send + Sync` cannot be known at compilation time
   --> src/error.rs:150:30
    |
150 |             Error::construct(boxed_error, vtable, bt)
    |             ---------------- ^^^^^^^^^^^ doesn't have a size known at compile-time
    |
    = help: the trait `Sized` is not implemented for `dyn core::error::Error + Send + Sync`
help: the trait `core::error::Error` is implemented for `Box<E>`
    = note: required for `Box<dyn core::error::Error + Send + Sync>` to implement `core::error::Error`
note: required by a bound in `construct`
   --> src/error.rs:245:8
    |
245 |     E: StdError + Send + Sync + 'static,
    |        ^^^^^^^^ required by this bound in `construct`
"""

_HOLDER = TypeRecord(
    file="src/wrap.rs", kind="struct", name="Held",
    fields=(FieldRecord(name="inner", ty="Box<dyn StdError + Send + Sync>"),),
    trait_impls=(TraitImplRecord(
        header="impl StdError for Held", trait_path="StdError",
        self_ty="Held"),),
)
_PLAIN = TypeRecord(
    file="src/wrap.rs", kind="struct", name="Plain",
    fields=(FieldRecord(name="n", ty="u32"),),
    trait_impls=(),
)
_SIG = "fn from_boxed(boxed_error: Box<dyn StdError + Send + Sync>) -> Self"


def test_the_holder_is_named_and_the_signature_walk_misses_it() -> None:
    got = adapter_types_for_diagnostic(_E0277, [_HOLDER, _PLAIN])
    assert [r.name for r in got] == ["Held"]
    named = [r.name for r in types_referenced_in_text(
        _SIG, [_HOLDER, _PLAIN])]
    assert "Held" not in named


def test_the_retry_appendix_carries_the_holder() -> None:
    text = with_blamed_type_api(_E0277, [_HOLDER])
    assert "`Held`" in text and "src/wrap.rs" in text


def test_a_diagnostic_without_the_code_adds_nothing() -> None:
    assert with_blamed_type_api("error[E0308]: mismatched types", [_HOLDER]) == (
        "error[E0308]: mismatched types")


# The recorded block: run 371055-1a407b3c, file retry prompt `19fa55ff…`
# (the same block reached the session turns). Records spelled the way the
# extractor spelled them on that tree (`CliStructureReader.enumerate_types`
# over `.skeleton-filled/stable`, 2026-09-28): the wrapper's tuple field,
# and a chain type whose field is a borrowed trait object with no `Send`.
_RECORDED = (Path(__file__).parent / "fixtures" / "e0277"
             / "unsized_boxed_dyn_error_371055.txt").read_text()
_WRAPPER = TypeRecord(
    file="src/wrapper.rs", kind="struct", name="BoxedError",
    fields=(FieldRecord(name="0", ty="Box < dyn StdError + Send + Sync >"),),
    trait_impls=tuple(
        TraitImplRecord(header=f"impl {t} for BoxedError", trait_path=t,
                        self_ty="BoxedError")
        for t in ("Debug", "Display", "StdError")),
)
_CHAIN = TypeRecord(
    file="src/chain.rs", kind="enum", name="ChainState",
    fields=(FieldRecord(name="next",
                        ty="Option < & 'a (dyn StdError + 'static) >"),),
    trait_impls=(),
)
_FROM_BOXED = ("fn from_boxed(boxed_error: Box<dyn StdError + Send + Sync + "
               "'static>) -> Self")


def test_the_recorded_diagnostic_names_the_wrapper_and_only_it() -> None:
    """On the live records, 14 of the 15 recorded prompts carrying an E0277
    block gave `BoxedError`; this is that block. The signature walk, the
    selection every failing prompt had, does not return it."""
    got = adapter_types_for_diagnostic(_RECORDED, [_WRAPPER, _CHAIN, _PLAIN])
    assert [r.name for r in got] == ["BoxedError"]
    named = [r.name for r in types_referenced_in_text(
        _FROM_BOXED, [_WRAPPER, _CHAIN, _PLAIN])]
    assert "BoxedError" not in named
    appendix = with_blamed_type_api(_RECORDED, [_WRAPPER, _CHAIN])
    assert "`BoxedError` in `src/wrapper.rs`" in appendix


def test_negative_control_a_holder_without_the_trait_is_not_offered() -> None:
    """Holding the value is half the answer: a type that does not implement
    the trait the bound names cannot be passed where it is required."""
    bare = TypeRecord(
        file="src/wrapper.rs", kind="struct", name="BoxedError",
        fields=_WRAPPER.fields, trait_impls=())
    assert adapter_types_for_diagnostic(_RECORDED, [bare]) == ()


# Invented shapes for the other forms rustc prints an E0277 in. No crate
# symbol: a `Speak` trait, a `Voice` value, the holders around it.
def _rec(name: str, field_ty: str, *traits: str,
         impls: bool = True) -> TypeRecord:
    return TypeRecord(
        file="src/h.rs", kind="struct", name=name,
        fields=(FieldRecord(name="0", ty=field_ty),),
        trait_impls=(tuple(TraitImplRecord(
            header=f"impl {t} for {name}", trait_path=f"crate::{t}",
            self_ty=name) for t in traits) if impls else None),
    )


_BOUND = """\
error[E0277]: the trait bound `Voice: Speak` is not satisfied
  --> src/a.rs:3:9
   |
 3 |     say(v)
   |     --- ^ the trait `Speak` is not implemented for `Voice`
"""


def test_the_bound_form_names_a_holder_of_the_failing_type() -> None:
    held = _rec("Loud", "Voice", "Speak")
    boxed = _rec("Boxed", "Box<Voice>", "Speak")
    mute = _rec("Mute", "Voice", "Listen")
    unknown = _rec("Opaque", "Voice", impls=False)
    got = adapter_types_for_diagnostic(_BOUND, [held, boxed, mute, unknown])
    assert [r.name for r in got] == ["Loud", "Boxed"]


def test_a_boxed_trait_object_subject_matches_under_the_bound_spelling() -> None:
    diag = """\
error[E0277]: the trait bound `Box<dyn Speak + Send>: Speak` is not satisfied
   = help: the trait `crate::Speak` is not implemented for `Box<dyn Speak + Send>`
    |
 12 |     T: Speak + Send,
"""
    holder = _rec("Wrap", "Box<dyn Speak + Send>", "Speak")
    wrong_autos = _rec("Other", "Box<dyn Speak>", "Speak")
    assert [r.name for r in adapter_types_for_diagnostic(
        diag, [holder, wrong_autos])] == ["Wrap"]


def test_shapes_that_name_no_holder_add_nothing() -> None:
    held = _rec("Loud", "Voice", "Speak")
    two_principals = (
        "error[E0277]: the size for values of type `dyn Speak + Listen` "
        "cannot be known at compilation time\n")
    not_dyn = (
        "error[E0277]: the size for values of type `[u8]` cannot be known "
        "at compilation time\n")
    no_colon = "error[E0277]: the trait bound `Voice` is not satisfied\n"
    no_form = "error[E0277]: `Voice` cannot be sent between threads safely\n"
    for diag in (two_principals, not_dyn, no_colon, no_form):
        assert adapter_types_for_diagnostic(diag, [held]) == (), diag
    only_autos = (
        "error[E0277]: the size for values of type `dyn Send + Sync` cannot "
        "be known at compilation time\n")
    assert adapter_types_for_diagnostic(only_autos, [held]) == ()


def test_a_holder_named_by_two_errors_is_listed_once() -> None:
    held = _rec("Loud", "Voice", "Speak")
    assert [r.name for r in adapter_types_for_diagnostic(
        _BOUND + _BOUND, [held])] == ["Loud"]
