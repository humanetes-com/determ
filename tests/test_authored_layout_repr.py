"""A re-authored type keeps the memory representation the crate declares.

The crash retrigger of run `241132-f9fae0a6` re-authored `stable`'s
Error/ErrorImpl component on 2026-08-07 and returned `pub struct Error` without
the `#[repr(transparent)]` its frozen definition carries. The cluster then could
not build at all — `tests/test_ffi.rs:7: error: extern fn uses type
anyhow::Error, which is not FFI-safe` — with 56 fns already reverted to stubs
against the new layout. The accept gate could not see it: `cargo check --lib`
inside `.skeleton/<cluster>`, which holds no `tests/` at all, returns 0 on that
layout.
"""
from __future__ import annotations

from pipeline_core.domain.authored_layout import carry_frozen_repr

_FROZEN_ERROR = """/// The `Error` type, a wrapper around a dynamic error type.
#[repr(transparent)]
pub struct Error {
    inner: Own<ErrorImpl>,
}"""


def test_dropped_repr_is_carried_back() -> None:
    authored = "pub struct Error {\n    inner: Own<ErrorImpl>,\n}"
    out = carry_frozen_repr(_FROZEN_ERROR, authored)
    assert out == "#[repr(transparent)]\npub struct Error {\n    inner: Own<ErrorImpl>,\n}"


def test_doc_comments_and_other_attrs_stay_above_the_repr() -> None:
    """The repr binds to the item, so it goes on the declaration line — not at
    the top of a block that opens with docs or a derive."""
    authored = ("/// Re-authored.\n#[derive(Debug)]\npub struct Error {\n"
                "    inner: Own<ErrorImpl>,\n}")
    out = carry_frozen_repr(_FROZEN_ERROR, authored)
    assert out == ("/// Re-authored.\n#[derive(Debug)]\n#[repr(transparent)]\n"
                   "pub struct Error {\n    inner: Own<ErrorImpl>,\n}")


def test_a_repr_the_model_wrote_is_left_alone() -> None:
    """One-directional on purpose: re-representing a type is a decision the
    compile gate can judge, and overwriting it could emit `transparent` and `C`
    on the same item, which is not a layout at all."""
    authored = "#[repr(C)]\npub struct Error {\n    inner: Own<ErrorImpl>,\n}"
    assert carry_frozen_repr(_FROZEN_ERROR, authored) == authored


def test_frozen_without_a_repr_adds_nothing() -> None:
    frozen = "struct ErrorVTable {\n    object_drop: unsafe fn(Own<ErrorImpl>),\n}"
    authored = "struct ErrorVTable {\n    object_drop: unsafe fn(Own<ErrorImpl>),\n    x: u8,\n}"
    assert carry_frozen_repr(frozen, authored) == authored


def test_repr_c_is_carried_too() -> None:
    """`ErrorImpl` and `ContextError` are `#[repr(C)]` to hold a field in its
    final position — losing that one reorders fields under the pointer casts
    that read them instead of failing to compile."""
    frozen = "#[repr(C)]\npub(crate) struct ContextError<C, E> {\n    pub context: C,\n    pub error: E,\n}"
    authored = "pub(crate) struct ContextError<C, E> {\n    pub context: C,\n    pub error: E,\n}"
    assert carry_frozen_repr(frozen, authored).startswith("#[repr(C)]\npub(crate) struct")


def test_an_indented_item_keeps_its_indent() -> None:
    frozen = "    #[repr(transparent)]\n    struct Own<T> {\n        ptr: NonNull<T>,\n    }"
    authored = "    struct Own<T> {\n        ptr: NonNull<T>,\n    }"
    out = carry_frozen_repr(frozen, authored)
    assert out.splitlines()[0] == "    #[repr(transparent)]"
    assert out.splitlines()[1] == "    struct Own<T> {"


def test_a_definition_with_no_item_declaration_is_untouched() -> None:
    """Type aliases and fns are authored through the same protocol; there is no
    declaration line to bind a repr to, so nothing happens."""
    frozen = "#[repr(transparent)]\npub struct Error { inner: Own<ErrorImpl> }"
    authored = "type Result<T> = core::result::Result<T, Error>;"
    assert carry_frozen_repr(frozen, authored) == authored


# --- the splice actually applies it -------------------------------------------

def test_the_commit_writes_and_returns_the_carried_repr() -> None:
    """End-to-end over `_try_commit`: the carry has to reach the file the
    accept gate compiles AND the text the caller stores (registry, locate
    keys, authored items). A splice that carried only into the file it wrote
    left the stores on the model's text, and the cluster-start
    reconciliation reinstalls a stored definition the live tree does not
    contain verbatim (`plan_layout_reconciliation`)."""
    from pipeline_core.application.author_layout import (
        AuthorLayoutDeps,
        _try_commit,
    )
    from pipeline_core.domain.authored_layout import AuthoredItem
    from pipeline_core.domain.layout_probe import LayoutGate
    from pipeline_core.domain.private_surface import (
        PrivateSurfaceItem,
        SurfaceItemKind,
    )

    skeleton = f"pub use anyhow as format_err;\n{_FROZEN_ERROR}\n"
    written: dict[str, str] = {}
    items = (PrivateSurfaceItem(kind=SurfaceItemKind.PUB_FIELDS, name="Error",
                                file="src/lib.rs", definition=_FROZEN_ERROR),)
    authored = {"Error": AuthoredItem(
        "Error", "struct",
        "pub struct Error {\n    inner: Own<ErrorImpl>,\n}")}
    deps = AuthorLayoutDeps(
        run_model=lambda p: "", read_skeleton=lambda rel: skeleton,
        write_skeleton=written.__setitem__,
        compile_check=lambda: (True, ""), log=lambda m: None)

    tried = _try_commit(items, authored, deps, LayoutGate(), {})
    assert tried.committed
    assert "#[repr(transparent)]\npub struct Error {" in written["src/lib.rs"]
    assert tried.authored["Error"].definition.startswith(
        "#[repr(transparent)]\npub struct Error {")
    # The caller's candidate is not the stored text.
    assert not authored["Error"].definition.startswith("#[repr")


def test_contract_line_stays_above_the_carried_repr() -> None:
    """An authored block that opens with its `///` contract line and no repr
    gets the frozen repr inserted between the docs and the declaration — the
    docs are kept, and the repr still binds to the item."""
    authored = ("/// The TypeId stored is the message's own type, so downcast::<&str>() finds it.\n"
                "pub struct Error {\n    inner: Own<ErrorImpl>,\n}")
    out = carry_frozen_repr(_FROZEN_ERROR, authored)
    assert out == ("/// The TypeId stored is the message's own type, so downcast::<&str>() finds it.\n"
                   "#[repr(transparent)]\n"
                   "pub struct Error {\n    inner: Own<ErrorImpl>,\n}")


def test_a_public_type_keeps_its_frozen_derive_list() -> None:
    """A derive on a public type is public API. Run `42057-641e589c`
    (2026-10-26): the reply for `Error` carried `#[derive(Debug, PartialEq)]`
    against a crate whose `Debug` is hand-written (E0119) and which has no
    `PartialEq`; the head check skips attributes, so it landed."""
    from pipeline_core.domain.authored_layout import carry_frozen_derives

    reply = ("/// Pointer-sized handle.\n#[repr(transparent)]\n"
             "#[derive(Debug, PartialEq)]\npub struct Error {\n    ptr: NonNull<()>,\n}")
    frozen_none = "#[repr(transparent)]\npub struct Error {}"
    out = carry_frozen_derives(frozen_none, reply)
    assert "derive" not in out and "#[repr(transparent)]" in out and "ptr: NonNull<()>" in out
    frozen_clone = "#[derive(Clone)]\npub struct Error {}"
    out = carry_frozen_derives(frozen_clone, reply)
    assert out.count("#[derive(") == 1 and "#[derive(Clone)]\npub struct Error" in out
    assert carry_frozen_derives(frozen_clone, out) == out, "idempotent"
    # A derive inside a doc comment is not an attribute.
    doc = "/// #[derive(Debug)] is shown here\npub struct Error { a: u8 }"
    assert carry_frozen_derives(frozen_none, doc) == doc


def test_only_a_public_types_derives_are_carried() -> None:
    """Through `carry_frozen_facts`, the one text every consumer stores: a
    private struct's derives stay the model's choice."""
    from pipeline_core.domain.authored_layout import AuthoredItem, carry_frozen_facts
    from pipeline_core.domain.models import FieldRecord, TypeRecord
    from pipeline_core.domain.private_surface import (
        SurfaceItemKind,
        classify_private_surface,
    )

    surface = classify_private_surface([
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }",
                   fields=(FieldRecord(name="inner", ty="Box<ErrorImpl>", is_pub=False),)),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ], [], cfg_cluster="stable", author_private_fields=True)
    comp = next(c for c in surface.components if any(i.name == "Error" for i in c))
    kinds = {i.name: i.kind for i in comp}
    assert kinds["Error"] is SurfaceItemKind.PUB_FIELDS
    authored = {
        "Error": AuthoredItem("Error", "struct",
                              "#[derive(Debug, PartialEq)]\npub struct Error { p: Box<ErrorImpl> }"),
        "ErrorImpl": AuthoredItem("ErrorImpl", "struct",
                                  "#[derive(Debug)]\nstruct ErrorImpl { v: u8 }"),
    }
    out = carry_frozen_facts(comp, authored)
    assert "derive" not in out["Error"].definition
    assert out["ErrorImpl"].definition.startswith("#[derive(Debug)]")
