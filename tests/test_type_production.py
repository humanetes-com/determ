"""Pins for `domain.type_production` — the two rules that keep a futility
escalation from naming a third of the crate.

Both defects were live on 2026-08-01: on the `anyhow` tree `Error` escalated to
40 of 186 fns, and `E` — the generic parameter of `impl<E> StdError for E`, not
a type — to 33, because the subject was unfiltered and the match was a substring.
"""
from __future__ import annotations

from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.type_production import (
    EMPTY,
    ProducibleTypes,
    constructs_type,
    return_type_text,
    returns_type,
)


# ── the subject of an escalation ────────────────────────────────────────────
def test_only_structs_and_enums_are_producible() -> None:
    """A trait is implemented and an alias is a spelling; neither is produced.

    All four of these are real `anyhow` declarations, and all four arrived as
    escalation subjects from `container_type_name` before this filter."""
    recs = [
        TypeRecord(kind="struct", name="Error"),
        TypeRecord(kind="enum", name="ChainState"),
        TypeRecord(kind="trait", name="Context"),
        TypeRecord(kind="type", name="Result"),
    ]
    types = ProducibleTypes.from_type_records(recs)
    assert "Error" in types
    assert "ChainState" in types
    assert "Context" not in types
    assert "Result" not in types


def test_a_generic_parameter_is_not_a_declared_type() -> None:
    """`impl<E> StdError for E` yields the subject `E`, which denotes every type
    and therefore none. The extractor drops the impl's generic list from
    `container`, so only the crate's declarations can rule it out."""
    types = ProducibleTypes.from_type_records([TypeRecord(kind="struct", name="Error")])
    assert "E" not in types
    assert types.filter({"Error", "E", "Option", "bool"}) == frozenset({"Error"})


def test_unreadable_types_escalate_on_nothing() -> None:
    """No enumeration means no subject — this path's only failure mode is
    over-widening, so it fails toward silence, not toward an unfiltered set."""
    assert ProducibleTypes.from_type_records(None) is EMPTY
    assert not EMPTY
    assert EMPTY.filter({"Error"}) == frozenset()


def test_type_records_may_be_wire_dicts() -> None:
    types = ProducibleTypes.from_type_records(
        [{"kind": "struct", "name": "Own"}, {"kind": "trait", "name": "Sealed"}])
    assert types.names == frozenset({"Own"})


# ── the match ───────────────────────────────────────────────────────────────
def test_return_type_match_is_a_token_not_a_substring() -> None:
    """The live case: `E` must not match `Error` inside a return clause."""
    assert returns_type("fn f() -> Result<T, Error>", "Error")
    assert not returns_type("fn f() -> Result<T, Error>", "E")
    assert not returns_type("fn f() -> ErrorImpl<E>", "Error")


def test_self_return_counts_only_from_the_types_own_impl() -> None:
    assert returns_type("pub fn new(p: Box<T>) -> Self", "Own", own_type="Own")
    assert not returns_type("pub fn new(p: *const T) -> Self", "Own", own_type="Ref")


def test_a_unit_returning_fn_produces_nothing() -> None:
    assert return_type_text("fn f(&self)") == ""
    assert not returns_type("fn f(&self)", "Error")


def test_construction_match_is_a_token_not_a_substring() -> None:
    """`MyError { … }` is not a construction of `Error`."""
    assert constructs_type("let e = Error { inner };", "Error")
    assert not constructs_type("let e = MyError { inner };", "Error")
    assert not constructs_type("let e = Error::msg(x);", "Error")


def test_construction_tolerates_pretty_printer_spacing() -> None:
    """Bodies are re-emitted by `prettyplease`, which chooses its own spacing."""
    assert constructs_type("Error\n    { inner }", "Error")


def test_empty_name_never_matches() -> None:
    assert not returns_type("fn f() -> Error", "")
    assert not constructs_type("Error { }", "")


def test_tuple_struct_construction_is_a_call_and_only_when_asked() -> None:
    """A tuple struct is constructed by a call — `MessageError(message)` — so
    the call form counts iff the caller says the type IS a tuple struct
    (`tuple_struct=True`, from the record's unnamed fields). The default stays
    brace-only, byte-identical for every existing consumer."""
    body = "Self::construct_from_std(crate::wrapper::MessageError(message), bt)"
    assert constructs_type(body, "MessageError", tuple_struct=True)
    assert not constructs_type(body, "MessageError")
    # A path call on the type is not a construction under either setting.
    assert not constructs_type("Error::msg(x)", "Error", tuple_struct=True)
    # Token-exact under the call form too: `MyMessageError(x)` is not it.
    assert not constructs_type("MyMessageError(x)", "MessageError", tuple_struct=True)
    # Pretty-printer spacing between name and paren is tolerated.
    assert constructs_type("MessageError (message)", "MessageError", tuple_struct=True)
