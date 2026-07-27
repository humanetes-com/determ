"""Pins for the F11 authored-layout reply protocol + coverage check."""
from __future__ import annotations

from pipeline_core.domain.authored_layout import (
    layout_coverage,
    parse_authored_layout,
)
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)

_REPLY = '''\
Here is the private layout I designed:
<LAYOUT_ITEM name="ErrorImpl" kind="struct">
struct ErrorImpl { inner: Box<dyn StdError + Send + Sync>, backtrace: Option<Backtrace> }
</LAYOUT_ITEM>
<LAYOUT_ITEM name="Own" kind="struct">
struct Own<T> { ptr: Box<T> }
</LAYOUT_ITEM>
'''


def test_parses_items_in_order():
    lay = parse_authored_layout(_REPLY)
    assert [i.name for i in lay.items] == ["ErrorImpl", "Own"]
    assert lay.items[0].kind == "struct"
    assert "Box<dyn StdError" in lay.items[0].definition
    assert lay.item("Own") is not None


def test_html_escaped_generics_decoded():
    reply = ('<LAYOUT_ITEM name="Own" kind="struct">'
             'struct Own&lt;T&gt; { ptr: Box&lt;T&gt; }</LAYOUT_ITEM>')
    lay = parse_authored_layout(reply)
    assert lay.items[0].definition == "struct Own<T> { ptr: Box<T> }"


def test_malformed_block_skipped_not_fatal():
    reply = ('<LAYOUT_ITEM kind="struct">no name</LAYOUT_ITEM>'
             '<LAYOUT_ITEM name="Good" kind="struct">struct Good;</LAYOUT_ITEM>')
    lay = parse_authored_layout(reply)
    assert [i.name for i in lay.items] == ["Good"]


def test_coverage_reports_missing_and_extra():
    required = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs", ""),
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorVTable", "src/error.rs", ""),
    )
    lay = parse_authored_layout(_REPLY)  # authors ErrorImpl + Own
    missing, extra = layout_coverage(required, lay)
    assert missing == frozenset({"ErrorVTable"})
    assert extra == frozenset({"Own"})


def test_full_coverage_is_empty():
    required = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs", ""),
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "Own", "src/ptr.rs", ""),
    )
    missing, extra = layout_coverage(required, parse_authored_layout(_REPLY))
    assert missing == frozenset() and extra == frozenset()


def test_derive_locate_keys_from_scaffold_records():
    from pipeline_core.domain.authored_layout import derive_locate_keys
    from pipeline_core.domain.models import FnRecord, TypeRecord
    items = (
        PrivateSurfaceItem(kind=SurfaceItemKind.STRUCT, name="Own",
                           file="src/ptr.rs", definition="stale-baked-def"),
        # two same-named fns in one file — must bind by source order
        PrivateSurfaceItem(kind=SurfaceItemKind.FN_SIG, name="as_ref",
                           file="src/ptr.rs", definition="stale-sig-a"),
        PrivateSurfaceItem(kind=SurfaceItemKind.FN_SIG, name="as_ref",
                           file="src/ptr.rs", definition="stale-sig-b"),
    )
    fns = [
        FnRecord(file="src/ptr.rs", fn_name="as_ref", is_exactly_pub=False,
                 sig_text="fn as_ref(&self) -> Ref<T>", body_text=""),
        FnRecord(file="src/ptr.rs", fn_name="as_ref", is_exactly_pub=False,
                 sig_text="fn as_ref(&self) -> &T", body_text=""),
        FnRecord(file="src/other.rs", fn_name="as_ref", is_exactly_pub=False,
                 sig_text="fn as_ref(&self) -> u8", body_text=""),
    ]
    types = [TypeRecord(file="src/ptr.rs", kind="struct", name="Own",
                        is_pub=False, definition="struct Own<T> { ptr: *mut T }")]
    keys = derive_locate_keys(items, fns, types)
    assert keys is not None
    got = dict(keys)
    assert got["Own"] == "struct Own<T> { ptr: *mut T }"
    assert got["as_ref.1"] == "fn as_ref(&self) -> Ref<T>"
    assert got["as_ref.2"] == "fn as_ref(&self) -> &T"


def test_derive_locate_keys_never_binds_a_frozen_public_twin():
    """A private item whose name is also a PUBLIC method's in the same file must
    bind the private record, whatever the source order — binding the public one
    makes the caller splice the layout over the frozen API and delete it."""
    from pipeline_core.domain.authored_layout import (
        derive_locate_keys,
        unlocatable_private_fns,
    )
    from pipeline_core.domain.models import FnRecord
    items = (PrivateSurfaceItem(kind=SurfaceItemKind.FN_SIG, name="backtrace",
                                file="error.rs", definition="stale"),)
    public_first = [
        FnRecord(file="error.rs", fn_name="backtrace", is_exactly_pub=True,
                 container="impl Error",
                 sig_text="fn backtrace(&self) -> &Backtrace"),
        FnRecord(file="error.rs", fn_name="backtrace", is_exactly_pub=False,
                 container="impl ErrorImpl",
                 sig_text="fn backtrace(this: Ref<Self>) -> &Backtrace"),
    ]
    keys = derive_locate_keys(items, public_first, [])
    assert keys is not None
    assert dict(keys)["backtrace"] == "fn backtrace(this: Ref<Self>) -> &Backtrace"
    # only a public record present → refuse rather than overwrite the API
    assert derive_locate_keys(items, public_first[:1], []) is None
    assert unlocatable_private_fns(items, public_first[:1]) == ("backtrace",)


def test_derive_locate_keys_none_on_any_miss():
    from pipeline_core.domain.authored_layout import derive_locate_keys
    from pipeline_core.domain.models import TypeRecord
    items = (
        PrivateSurfaceItem(kind=SurfaceItemKind.STRUCT, name="Own",
                           file="src/ptr.rs", definition="d"),
        PrivateSurfaceItem(kind=SurfaceItemKind.FN_SIG, name="erase",
                           file="src/ptr.rs", definition="s"),
    )
    types = [TypeRecord(file="src/ptr.rs", kind="struct", name="Own",
                        is_pub=False, definition="struct Own;")]
    # fn record absent → whole derivation refuses (no partial guessing)
    assert derive_locate_keys(items, [], types) is None
    # kind mismatch (trait vs struct) refuses too
    items2 = (PrivateSurfaceItem(kind=SurfaceItemKind.TRAIT, name="Own",
                                 file="src/ptr.rs", definition="d"),)
    assert derive_locate_keys(items2, [], types) is None
