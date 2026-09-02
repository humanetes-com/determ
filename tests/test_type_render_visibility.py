"""`format_type_for_prompt` renders a struct's fields that are private to
another file as NOT accessible from the file the prompt writes, with the
accessor that returns each one — and keeps the direct-access rendering for
the declaring file and for every caller that names no file.

Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the paired fill prompt
for `src/error.rs::fmt` + `src/fmt.rs::debug` rendered `ErrorImpl` —
declared in `src/error.rs`, fields `vtable`, `backtrace`, `_object`, none
`pub` — under `fields (access with .name):` 15 times. The model, writing the
`src/fmt.rs` half, read `impl_ref.backtrace` directly: `error[E0616]: field
backtrace of struct ErrorImpl is private` on 3 of its 6 attempts (two on
`backtrace`, one on `vtable`), while the record's own
`unsafe fn backtrace(this: Ref<Self>) -> &Backtrace` was listed a few lines
below under methods. `ERROR_IMPL` below is that record's recorded shape.
"""
from __future__ import annotations

from pipeline_core.domain.models import FieldRecord, MethodRecord, TypeRecord
from pipeline_core.domain.type_refs import (
    accessor_methods_for_field,
    field_accessible_from,
)
from pipeline_core.prompts.sections import format_type_for_prompt

ERROR_IMPL = TypeRecord(
    kind="struct", name="ErrorImpl", generics="<E = ()>", file="src/error.rs",
    definition=(
        "#[repr(C)]\npub(crate) struct ErrorImpl<E = ()> {\n"
        "    vtable: &'static ErrorVTable,\n"
        "    backtrace: Option<Backtrace>,\n"
        "    _object: E,\n}"
    ),
    fields=(
        FieldRecord(name="vtable", ty="&'static ErrorVTable", is_pub=False),
        FieldRecord(name="backtrace", ty="Option<Backtrace>", is_pub=False),
        FieldRecord(name="_object", ty="E", is_pub=False),
    ),
    methods=(
        MethodRecord(
            name="error",
            sig="unsafe fn error(this: Ref<Self>) -> &(dyn StdError + Send + Sync + 'static)",
            is_pub=True, is_exactly_pub=False, file="src/error.rs"),
        MethodRecord(
            name="backtrace",
            sig="unsafe fn backtrace(this: Ref<Self>) -> &Backtrace",
            is_pub=True, is_exactly_pub=False, file="src/error.rs"),
        MethodRecord(
            name="chain", sig="unsafe fn chain(this: Ref<Self>) -> Chain",
            is_pub=True, is_exactly_pub=False, file="src/error.rs"),
    ),
    is_pub=True,
)

VTABLE_FIELD, BACKTRACE_FIELD, OBJECT_FIELD = ERROR_IMPL.fields

ACCESS_HEADER = "  fields (access with .name):"


def _private_header(out: str) -> str:
    headers = [ln for ln in out.splitlines() if ln.startswith("  private fields")]
    assert len(headers) == 1, out
    return headers[0]


def _line_starting(out: str, prefix: str) -> str:
    hits = [ln for ln in out.splitlines() if ln.startswith(prefix)]
    assert len(hits) == 1, (prefix, out)
    return hits[0]


# --- the render for another file -------------------------------------------

def test_other_file_renders_every_private_field_under_the_not_accessible_header() -> None:
    out = format_type_for_prompt(ERROR_IMPL, viewer_file="src/fmt.rs")
    # All three fields are private to src/error.rs: no direct-access header.
    assert ACCESS_HEADER not in out
    header = _private_header(out)
    assert "declared in src/error.rs" in header
    assert "NOT accessible from src/fmt.rs" in header
    assert "E0616" in header and "E0451" in header
    lines = out.splitlines()
    backtrace = _line_starting(out, "    backtrace: Option<Backtrace>")
    assert lines.index(backtrace) > lines.index(header)
    assert backtrace.endswith("→ unsafe fn backtrace(this: Ref<Self>) -> &Backtrace")
    assert "construct:" not in out
    # The method list is untouched: the accessor is still there too.
    assert "  methods (sig shown; call with .name(args)):" in out


def test_other_file_marks_fields_no_method_returns() -> None:
    out = format_type_for_prompt(ERROR_IMPL, viewer_file="src/fmt.rs")
    vtable = _line_starting(out, "    vtable: &'static ErrorVTable")
    # `error` returns `&(dyn StdError + Send + Sync + 'static)`: a shared
    # lifetime is not a shared type, so it is not the vtable's accessor.
    assert vtable.endswith("→ (no accessor declared here)")
    assert "fn error(" not in vtable
    assert _line_starting(out, "    _object: E").endswith(
        "→ (no accessor declared here)")


def test_private_header_says_no_method_returns_them_when_none_does() -> None:
    rec = TypeRecord(kind="struct", name="Own", file="src/ptr.rs",
                     definition="pub struct Own<T> { ptr: NonNull<T> }",
                     fields=(FieldRecord(name="ptr", ty="NonNull<T>"),))
    out = format_type_for_prompt(rec, viewer_file="src/error.rs")
    assert "no method of this type returns them" in _private_header(out)
    assert "    ptr: NonNull<T>   → (no accessor declared here)" in out
    assert "construct:" not in out


def test_private_field_line_joins_several_accessors() -> None:
    rec = TypeRecord(
        kind="struct", name="Holder", file="src/a.rs",
        fields=(FieldRecord(name="bt", ty="Option<Backtrace>"),),
        methods=(MethodRecord(name="bt", sig="fn bt(&self) -> &Backtrace"),
                 MethodRecord(name="take_bt",
                              sig="fn take_bt(self) -> Option<Backtrace>")))
    out = format_type_for_prompt(rec, viewer_file="src/b.rs")
    assert ("    bt: Option<Backtrace>   → fn bt(&self) -> &Backtrace / "
            "fn take_bt(self) -> Option<Backtrace>") in out


def test_tuple_struct_header_names_the_private_constructor() -> None:
    # The live `Quoted<C>(C)` record in src/context.rs: from another file the
    # constructor call `Quoted(x)` is E0603, not a struct-literal E0451.
    rec = TypeRecord(kind="struct", name="Quoted", generics="<C>",
                     file="src/context.rs", definition="struct Quoted<C>(C);",
                     fields=(FieldRecord(name="0", ty="C"),))
    out = format_type_for_prompt(rec, viewer_file="src/fmt.rs")
    header = _private_header(out)
    assert "the `Quoted(..)` constructor is E0603 there" in header
    assert "E0451" not in header
    assert "    0: C   → (no accessor declared here)" in out
    assert "construct:" not in out


def test_mixed_visibility_splits_pub_and_private_fields() -> None:
    rec = TypeRecord(
        kind="struct", name="Pair", file="src/a.rs",
        definition="pub struct Pair { pub left: u8, right: u8 }",
        fields=(FieldRecord(name="left", ty="u8", is_pub=True),
                FieldRecord(name="right", ty="u8")),
        methods=(MethodRecord(name="right", sig="fn right(&self) -> u8"),))
    out = format_type_for_prompt(rec, viewer_file="src/b.rs")
    lines = out.splitlines()
    i = lines.index(ACCESS_HEADER)
    assert lines[i + 1] == "    pub left: u8"
    assert lines[i + 2].startswith("  private fields")
    assert lines[i + 3] == "    right: u8   → fn right(&self) -> u8"
    # One private field is enough to make the struct literal unwritable there.
    assert "construct:" not in out


# --- the render for the declaring file and its descendants -----------------

def test_declaring_file_keeps_direct_access_and_the_construct_hint() -> None:
    out = format_type_for_prompt(ERROR_IMPL, viewer_file="src/error.rs")
    lines = out.splitlines()
    i = lines.index(ACCESS_HEADER)
    assert lines[i + 2] == "    backtrace: Option<Backtrace>"
    assert any(ln.startswith("  construct: ErrorImpl {") for ln in lines)
    assert "private fields" not in out
    assert out == format_type_for_prompt(ERROR_IMPL)


def test_descendant_module_file_reads_like_the_declaring_file() -> None:
    assert (format_type_for_prompt(ERROR_IMPL, viewer_file="src/error/inner.rs")
            == format_type_for_prompt(ERROR_IMPL, viewer_file="src/error.rs"))


def test_enum_and_trait_records_are_unaffected_by_viewer_file() -> None:
    kind = TypeRecord(kind="enum", name="Kind", file="src/a.rs",
                      variants=("A", "B"))
    marker = TypeRecord(kind="trait", name="Marker", file="src/a.rs",
                        methods=(MethodRecord(name="m", sig="fn m(&self)"),))
    for rec in (kind, marker):
        assert (format_type_for_prompt(rec, viewer_file="src/b.rs")
                == format_type_for_prompt(rec))


# --- negative control: the pre-fix rendering, kept for callers with no file -

def test_negative_control_no_viewer_file_renders_the_recorded_access_header() -> None:
    # This is the text the recorded prompt carried: every field, private or
    # not, under `fields (access with .name):`. Callers that name no file
    # must keep it byte for byte; a caller that does name one gets the split.
    out = format_type_for_prompt(ERROR_IMPL)
    lines = out.splitlines()
    i = lines.index(ACCESS_HEADER)
    assert lines[i + 1:i + 4] == [
        "    vtable: &'static ErrorVTable",
        "    backtrace: Option<Backtrace>",
        "    _object: E",
    ]
    assert "private fields" not in out
    assert "  construct: ErrorImpl {" in out
    assert out == format_type_for_prompt(ERROR_IMPL, viewer_file="")


# --- field_accessible_from ---------------------------------------------------

def test_field_accessible_from_file_rules() -> None:
    f = FieldRecord(name="x", ty="u8")
    rec = TypeRecord(kind="struct", name="S", file="src/error.rs", fields=(f,))
    assert field_accessible_from(rec, f, "")                        # no viewer
    assert field_accessible_from(TypeRecord(name="S", fields=(f,)), f,
                                 "src/fmt.rs")                      # unlocated record
    assert field_accessible_from(rec, f, "src/error.rs")            # same file
    assert field_accessible_from(rec, f, "src/error/inner.rs")      # child module
    assert field_accessible_from(rec, f, "src/error/deep/leaf.rs")  # grandchild
    assert not field_accessible_from(rec, f, "src/fmt.rs")          # another file
    assert not field_accessible_from(rec, f, "src/error_ext.rs")    # shares only the prefix
    # `pub(crate)`/`pub(super)` fold into is_pub=True: readable from another file.
    assert field_accessible_from(
        rec, FieldRecord(name="y", ty="u8", is_pub=True), "src/fmt.rs")


def test_field_accessible_from_crate_root_and_mod_rs() -> None:
    f = FieldRecord(name="inner", ty="Own<ErrorImpl>")
    root = TypeRecord(kind="struct", name="Error", file="src/lib.rs", fields=(f,))
    # Every module of the library descends from the crate root.
    assert field_accessible_from(root, f, "src/fmt.rs")
    assert field_accessible_from(root, f, "src/error/inner.rs")
    # An integration test is a separate crate.
    assert not field_accessible_from(root, f, "tests/test_fmt.rs")
    mod_rs = TypeRecord(kind="struct", name="S", file="src/error/mod.rs",
                        fields=(f,))
    assert field_accessible_from(mod_rs, f, "src/error/inner.rs")
    assert not field_accessible_from(mod_rs, f, "src/fmt.rs")


def test_field_accessible_from_tolerates_src_prefix_disagreement() -> None:
    f = FieldRecord(name="x", ty="u8")
    rec = TypeRecord(kind="struct", name="S", file="error.rs", fields=(f,))
    assert field_accessible_from(rec, f, "src/error.rs")
    assert field_accessible_from(rec, f, "src/error/inner.rs")
    assert not field_accessible_from(rec, f, "src/fmt.rs")


# --- accessor_methods_for_field ---------------------------------------------

def test_accessor_is_the_method_returning_the_field_type() -> None:
    assert [m.name for m in accessor_methods_for_field(ERROR_IMPL, BACKTRACE_FIELD)] \
        == ["backtrace"]


def test_no_accessor_when_nothing_returns_the_field_type() -> None:
    assert accessor_methods_for_field(ERROR_IMPL, OBJECT_FIELD) == ()


def test_no_accessor_on_a_shared_lifetime_alone() -> None:
    # `vtable: &'static ErrorVTable` vs `error() -> &(dyn StdError + Send +
    # Sync + 'static)`: `static` is a lifetime, not a type name.
    assert accessor_methods_for_field(ERROR_IMPL, VTABLE_FIELD) == ()


def test_no_accessor_on_a_shared_std_wrapper_alone() -> None:
    bt = FieldRecord(name="bt", ty="Option<Backtrace>")
    rec = TypeRecord(
        kind="struct", name="S", fields=(bt,),
        methods=(MethodRecord(name="len", sig="fn len(&self) -> Option<usize>"),
                 MethodRecord(name="bt", sig="fn bt(&self) -> Option<&Backtrace>")))
    assert [m.name for m in accessor_methods_for_field(rec, bt)] == ["bt"]


def test_accessor_reads_the_top_level_return_arrow_only() -> None:
    n = FieldRecord(name="n", ty="u8")
    rec = TypeRecord(
        kind="struct", name="S", fields=(n,),
        methods=(
            # arrow inside the parameter list, no return type
            MethodRecord(name="map", sig="fn map(&self, f: fn(u8) -> u8)"),
            # arrow inside the generics; the return type is `Wrapper`
            MethodRecord(name="apply",
                         sig="fn apply<F: Fn() -> u8>(&self, f: F) -> Wrapper"),
            # the where clause is not part of the return type
            MethodRecord(name="n", sig="fn n(&self) -> u8 where Self: Sized"),
        ))
    assert [m.name for m in accessor_methods_for_field(rec, n)] == ["n"]


def test_no_accessor_for_a_phantom_data_field() -> None:
    # The live `Ref<'a, T>` record: `lifetime: PhantomData<&'a T>` holds no
    # value; on the bare `T`, `deref(&self) -> &T` would read as its accessor.
    lifetime = FieldRecord(name="lifetime", ty="PhantomData < & 'a T >")
    rec = TypeRecord(
        kind="struct", name="Ref", file="src/ptr.rs",
        fields=(FieldRecord(name="ptr", ty="NonNull < T >", is_pub=True), lifetime),
        methods=(MethodRecord(name="deref", sig="fn deref (& self) -> & T"),
                 MethodRecord(name="by_mut", sig="fn by_mut (self) -> Mut < 'a , T >")))
    assert accessor_methods_for_field(rec, lifetime) == ()
    out = format_type_for_prompt(rec, viewer_file="src/fmt.rs")
    assert "    pub ptr: NonNull < T >" in out
    assert "    lifetime: PhantomData < & 'a T >   → (no accessor declared here)" in out


def test_accessor_dedups_a_method_recorded_once_per_impl_block() -> None:
    bt = FieldRecord(name="bt", ty="Backtrace")
    twice = MethodRecord(name="bt", sig="fn bt(&self) -> &Backtrace")
    rec = TypeRecord(kind="struct", name="S", fields=(bt,), methods=(twice, twice))
    assert len(accessor_methods_for_field(rec, bt)) == 1
