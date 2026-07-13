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
