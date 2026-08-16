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


def test_derive_traits_reads_every_plain_derive_attr():
    from pipeline_core.domain.authored_layout import derive_traits
    d = ("#[derive(Debug, Default)]\n"
         "#[derive(Clone)]\n"
         "struct Counter { count: u32 }")
    assert derive_traits(d) == frozenset({"Debug", "Default", "Clone"})
    # cfg_attr-wrapped derives are conditional and deliberately not read
    assert derive_traits(
        '#[cfg_attr(feature = "x", derive(Serialize))]\nstruct S;'
    ) == frozenset()
    assert derive_traits("struct S;") == frozenset()


def test_layout_regressions_names_lost_derives_and_lost_visibility():
    from pipeline_core.domain.authored_layout import layout_regressions
    cur = ("#[derive(Debug, Default, Clone, PartialEq, Eq)]\n"
           "pub struct Counter { count: u32 }")
    worse = "struct Counter { count: u32 }"
    regs = layout_regressions(cur, worse)
    assert any("Default" in r for r in regs)
    assert any("pub" in r and "private" in r for r in regs)
    # a superset of derives with the same visibility is not a regression,
    # whatever else the redesign changes
    better = ("/// doc\n#[derive(Debug, Default, Clone, PartialEq, Eq, Hash)]\n"
              "pub struct Counter { count: u64, cap: u32 }")
    assert layout_regressions(cur, better) == ()


# --- the locate-key verification that runs BEFORE an authoring call ---
#
# Both fixtures below are the live bytes of `tmp/anyhow` on 2026-08-16: the
# stored keys come from `.skeleton/_layout_outcomes_stable.json`, the file text
# from `.skeleton/stable/src/ptr.rs`. Those two rows are why every layout
# retrigger of cluster `stable` died in splice/locate infrastructure while the
# component the crash named had already authored a compiling layout.

_PTR_RS = (
    "#![allow(unused)]\n"
    "use core::marker::PhantomData;\n"
    "use core::ptr::NonNull;\n"
    "#[repr(transparent)]\n"
    "pub struct Own<T>\nwhere\n    T: ?Sized,\n{\n"
    "    pub ptr: core::ptr::NonNull<T>,\n}\n"
    "#[repr(transparent)]\n"
    "pub struct Mut<'a, T>\nwhere\n    T: ?Sized,\n{\n"
    "    pub ptr: NonNull<T>,\n"
    "    lifetime: PhantomData<&'a mut T>,\n}\n"
    "pub trait CastTo {\n    type Target;\n}\n"
)


def _item(name: str, definition: str, kind=None):
    from pipeline_core.domain.private_surface import SurfaceItemKind as K
    return PrivateSurfaceItem(
        name=name, file="ptr.rs", kind=kind or K.STRUCT, definition=definition)


def test_a_stored_key_carrying_an_item_the_file_lacks_is_unlocatable():
    """`CastTo`'s stored key is the trait FOLLOWED BY `impl<T> CastTo for T`.
    The skeleton holds the trait alone — the blanket impl exists only in the
    filled tree, where a fill wrote it — so a two-item key can never match."""
    from pipeline_core.domain.authored_layout import unlocatable_items
    from pipeline_core.domain.private_surface import SurfaceItemKind as K
    stale = ("pub trait CastTo {\n    type Target;\n}\n\n"
             "impl<T> CastTo for T {\n    type Target = T;\n}")
    assert unlocatable_items([_item("CastTo", stale, K.TRAIT)],
                             lambda rel: _PTR_RS) == ("CastTo",)
    # The trait alone — what the scaffold re-derivation produces — locates.
    assert unlocatable_items(
        [_item("CastTo", "pub trait CastTo {\n    type Target;\n}", K.TRAIT)],
        lambda rel: _PTR_RS) == ()


def test_a_qualified_path_does_not_match_the_imported_spelling():
    """`Mut`'s stored key spells `core::ptr::NonNull<T>` and the file, which
    imports `NonNull`, reads `NonNull<T>`. The matcher is whitespace-tolerant,
    not name-resolving — and it must not become so: two different paths CAN be
    two different types."""
    from pipeline_core.domain.authored_layout import unlocatable_items
    qualified = ("#[repr(transparent)]\npub struct Mut<'a, T>\nwhere\n"
                 "    T: ?Sized,\n{\n    pub ptr: core::ptr::NonNull<T>,\n"
                 "    lifetime: core::marker::PhantomData<&'a mut T>,\n}")
    assert unlocatable_items([_item("Mut", qualified)],
                             lambda rel: _PTR_RS) == ("Mut",)


def test_the_siblings_that_do_match_are_the_negative_control():
    """`Own` in the SAME file carries the qualified spelling in the skeleton
    too, so its stored key matches and its component authored fine every time.
    A verification that flagged it would be flagging the whole crate."""
    from pipeline_core.domain.authored_layout import unlocatable_items
    own = ("#[repr(transparent)]\npub struct Own<T>\nwhere\n    T: ?Sized,\n{\n"
           "    pub ptr: core::ptr::NonNull<T>,\n}")
    assert unlocatable_items([_item("Own", own)], lambda rel: _PTR_RS) == ()


def test_two_items_sharing_one_definition_need_two_occurrences():
    """Occurrence-stepped exactly like `replace_surface_definitions`: checking
    each item independently would pass a file holding one copy of a definition
    two items are going to be spliced into."""
    from pipeline_core.domain.authored_layout import unlocatable_items
    text = "fn helper() -> u8;\n"
    one = _item("helper", "fn helper() -> u8")
    assert unlocatable_items([one], lambda rel: text) == ()
    assert unlocatable_items([one, one], lambda rel: text) == ("helper",)


def test_an_unreadable_file_is_unlocatable_not_an_exception():
    """A missing skeleton file is the same finding as a missing definition —
    the splice cannot land — and it must be reported, never raised: this runs
    on the path that decides whether to spend a model call."""
    from pipeline_core.domain.authored_layout import unlocatable_items
    assert unlocatable_items([_item("Own", "struct Own;")],
                             lambda rel: None) == ("Own",)
