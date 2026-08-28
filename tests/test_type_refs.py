"""Direct unit tests for domain/type_refs.types_referenced_in_text.

The module was previously exercised only incidentally through e2e fill-loop
flows; these pin the pure contract directly (coverage ratchet)."""
from __future__ import annotations

from pipeline_core.domain.models import FieldRecord, MethodRecord, TypeRecord
from pipeline_core.domain.type_refs import (
    hidden_symbols_from_types,
    type_nameable_from,
    types_referenced_in_text,
)


def _rec(name: str, *fields: tuple[str, str]) -> TypeRecord:
    return TypeRecord(
        name=name, kind="struct",
        fields=tuple(FieldRecord(name=n, ty=t) for n, t in fields),
    )


ERROR = _rec("Error", ("inner", "Own<ErrorImpl>"))
OWN = _rec("Own", ("ptr", "NonNull<u8>"))
ERROR_IMPL = _rec("ErrorImpl", ("vtable", "&'static ErrorVTable"))
VTABLE = _rec("ErrorVTable")
BOXED = _rec("Boxed")


def test_empty_inputs_return_empty() -> None:
    assert types_referenced_in_text("", [ERROR]) == []
    assert types_referenced_in_text("fn f(e: Error)", []) == []


def test_direct_word_match_only() -> None:
    out = types_referenced_in_text("fn f(e: Error) -> Own<u8>", [ERROR, OWN, VTABLE])
    assert [r.name for r in out] == ["Error", "Own"]


def test_word_boundary_no_prefix_match() -> None:
    # `Boxed` in the text must not match record `Box`, and `Box` must not
    # pull in `Boxed`.
    box = _rec("Box")
    out = types_referenced_in_text("fn f(b: Boxed)", [box, BOXED])
    assert [r.name for r in out] == ["Boxed"]


def test_field_transitive_one_hop() -> None:
    # Error's field `Own<ErrorImpl>` pulls Own + ErrorImpl in; ErrorImpl's
    # own field (ErrorVTable) is a SECOND hop and stays out.
    out = types_referenced_in_text(
        "fn f(e: Error)", [ERROR, OWN, ERROR_IMPL, VTABLE])
    assert [r.name for r in out] == ["Error", "Own", "ErrorImpl"]


def test_hop_tokens_without_records_are_dropped() -> None:
    # NonNull/u8 appear in Own's field type but have no TypeRecord → dropped.
    out = types_referenced_in_text("fn f(o: Own<u8>)", [OWN, ERROR])
    assert [r.name for r in out] == ["Own"]


def test_duplicate_names_deduped() -> None:
    dup = _rec("Error")
    out = types_referenced_in_text("fn f(e: Error)", [ERROR, dup])
    assert len(out) == 1


def test_records_without_name_skipped() -> None:
    anon = TypeRecord(name="")
    out = types_referenced_in_text("fn f(e: Error)", [anon, ERROR])
    assert [r.name for r in out] == ["Error"]


def test_empty_field_ty_skipped() -> None:
    holder = _rec("Holder", ("x", ""))
    out = types_referenced_in_text("fn f(h: Holder)", [holder, ERROR])
    assert [r.name for r in out] == ["Holder"]


def test_hop_does_not_duplicate_direct_match() -> None:
    # Own matches directly AND appears in Error's field — listed once.
    out = types_referenced_in_text("fn f(e: Error, o: Own<u8>)", [ERROR, OWN])
    assert [r.name for r in out] == ["Error", "Own"]


def test_stderror_behind_private_mod_is_not_nameable_from_another_file() -> None:
    """The live TYPES_IN_SCOPE defect: `pub trait StdError` in `mod ext` of
    context.rs reached an error.rs prompt because `is_pub` is True and the
    renderer has no module wall. `visible_from` drops it for error.rs and
    keeps it for context.rs."""
    stderror = TypeRecord(
        name="StdError", kind="trait", file="src/context.rs",
        is_pub=True, mod_path="ext",
        methods=(MethodRecord(
            name="ext_context", sig="fn ext_context<C>(self, context: C) -> Error",
            is_pub=False, file="src/context.rs", mod_path="ext"),),
    )
    assert not type_nameable_from(stderror, "src/error.rs")
    assert type_nameable_from(stderror, "src/context.rs")
    from_error = types_referenced_in_text(
        "fn f(e: StdError)", [stderror, ERROR], visible_from="src/error.rs")
    assert [r.name for r in from_error] == []
    from_ctx = types_referenced_in_text(
        "fn f(e: StdError)", [stderror, ERROR], visible_from="src/context.rs")
    assert [r.name for r in from_ctx] == ["StdError"]
    assert from_ctx[0].methods[0].name == "ext_context"


def test_hidden_symbols_are_walled_methods_in_another_file() -> None:
    stderror = TypeRecord(
        name="StdError", kind="trait", file="src/context.rs",
        is_pub=True, mod_path="ext",
        methods=(MethodRecord(
            name="ext_context", sig="fn ext_context()",
            file="src/context.rs", mod_path="ext"),),
    )
    hidden = hidden_symbols_from_types([stderror], "src/error.rs")
    assert hidden == frozenset({"ext_context"})
    assert hidden_symbols_from_types([stderror], "src/context.rs") == frozenset()
