"""A fn-pointer field names the crate fns that fit it.

Of the compile errors quoted back to retries from 2026-09-01 to 2026-09-24,
143 named a fn that does not exist, nearly all fn-pointer table slots written
as if a fn of the slot's name existed (`object_drop_rest` 24 times; the
crate's fns of that signature are `object_drop_front`, `context_drop_rest`,
`context_chain_drop_rest`). The prompt listed the table's fields and every fn
and never said which fits which.

The same fixture exposed the comparison itself: on the live `stable` tree the
fields are spelled `crate::ptr::Own<crate::error::ErrorImpl>` while
`object_drop<E>` declares `e: Own<ErrorImpl>`, so the exact comparison the
installable offer and the session's table complement read matched nothing for
three of seven slots. The shapes below reproduce that spelling split on an
invented table.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.installable_fns import (
    _coercible,
    attach_slot_fits,
    fn_decl_shape,
    fn_pointer_shape,
    slot_fits,
)
from pipeline_core.domain.models import (
    BuildCluster,
    FieldRecord,
    FnRecord,
    SlotFit,
    TypeRecord,
)
from pipeline_core.prompts.sections import format_type_for_prompt

REL = "src/table.rs"
QUALIFIED = "unsafe fn (crate :: cell :: Slot < crate :: table :: Item > , core :: any :: TypeId)"


def _fn(name: str, sig: str, container: str = "", file: str = REL,
        cfg: tuple[str, ...] = ()) -> FnRecord:
    return FnRecord(file=file, fn_name=name, sig_text=sig, container=container,
                    cfg_predicates=cfg)


FNS = [
    _fn("drop_front", "fn drop_front<T>(s: Slot<Item>, id: TypeId)"),
    _fn("drop_rest", "unsafe fn drop_rest<A, B>(s: crate::cell::Slot<Item>, id: TypeId)"),
    _fn("any_first", "fn any_first<T>(x: T, id: TypeId)"),
    _fn("wrong_arity", "fn wrong_arity(s: Slot<Item>)"),
    _fn("make", "fn make(s: Slot<Item>, id: TypeId)", container="impl<A> Maker<A>"),
    _fn("method", "fn method(&self, id: TypeId)", container="impl Maker"),
    _fn("decl", "fn decl(s: Slot<Item>, id: TypeId)", container="trait Hook"),
    _fn("gated", "fn gated(s: Slot<Item>, id: TypeId)", cfg=("off_probe",)),
]


def test_the_exact_comparison_misses_a_path_spelled_field() -> None:
    """The comparison rule, checked against the same two spellings the live
    tree carries: a qualified field and an unqualified declaration."""
    field = fn_pointer_shape(QUALIFIED)
    decl = fn_decl_shape("fn drop_front<T>(s: Slot<Item>, id: TypeId)")
    assert field is not None and decl is not None
    assert field[1] != decl[1]           # the pre-change rule compared these
    assert _coercible(decl, field)       # one rule, path spelling aside


def test_slot_fits_lists_every_fitting_fn_spelled_for_a_literal() -> None:
    fits = slot_fits(QUALIFIED, FNS)
    assert fits is not None
    assert [f.name for f in fits] == [
        "drop_front<T>", "drop_rest<A, B>", "any_first<T>", "Maker::make", "gated"]
    assert fits[-1].cfg_predicates == ("off_probe",)


def test_not_a_fn_pointer_is_none_and_no_fit_is_empty() -> None:
    assert slot_fits("Vec<u8>", FNS) is None
    assert slot_fits("fn(u8, u16, u32) -> Item", FNS) == ()


def test_attach_skips_test_fns_and_keeps_other_fields_untouched() -> None:
    table = TypeRecord(file=REL, kind="struct", name="Table", fields=(
        FieldRecord(name="hook", ty=QUALIFIED),
        FieldRecord(name="len", ty="usize"),
    ))
    test_fn = FnRecord(file=REL, fn_name="t", sig_text="fn t(s: Slot<Item>, id: TypeId)",
                       is_test=True)
    (out,) = attach_slot_fits([table], [*FNS, test_fn])
    hook, length = out.fields
    assert hook.fits is not None and "t" not in [f.name for f in hook.fits]
    assert length.fits is None


def test_the_cluster_filter_drops_a_candidate_it_does_not_compile() -> None:
    off = BuildCluster(name="off", env_extra={}, tarpaulin_exclude_files=[],
                       excluded_cfg=["off_probe"], active_cfg=[],
                       prompt_nightly_rule="", is_default=True)
    (table,) = attach_slot_fits([TypeRecord(file=REL, kind="struct", name="Table",
                                            fields=(FieldRecord(name="hook", ty=QUALIFIED),))], FNS)
    (kept,) = cluster_filter_type_members([table], off)
    assert "gated" not in [f.name for f in kept.fields[0].fits or ()]


def test_the_type_section_prints_the_list_or_none_and_nothing_unread() -> None:
    table = TypeRecord(file=REL, kind="struct", name="Table", fields=(
        FieldRecord(name="hook", ty=QUALIFIED,
                    fits=(SlotFit("drop_front<T>", REL), SlotFit("other", "src/o.rs"))),
        FieldRecord(name="dead", ty="fn(u8)", fits=()),
        FieldRecord(name="unread", ty="fn(u16)"),
    ))
    out = format_type_for_prompt(table, viewer_file=REL)
    assert "COMPLETE list of crate fns it can hold" in out
    assert "      fits: drop_front<T>, other (src/o.rs)" in out
    assert "      fits: NONE — no crate fn is declared" in out
    # A record read without the crate's fns prints nothing for that field.
    assert sum(ln.startswith("      fits:") for ln in out.splitlines()) == 2
    plain = TypeRecord(file=REL, kind="struct", name="P",
                       fields=(FieldRecord(name="n", ty="u8"),))
    assert "  fields (access with .name):" in format_type_for_prompt(plain)


def test_a_type_parameter_binds_once_across_positions() -> None:
    """A bare type parameter binds to the type at its first position, and every
    later position must be that type. No return is `()`. Matching each
    position on its own listed `fn same<T>(a: T, b: T)` for `fn(u8, bool)`
    and `fn id<T>(x: T) -> T` for `fn(u8)`, neither of which can be stored,
    under a list headed COMPLETE (review of 2026-09-24)."""
    fns = [_fn("same", "fn same<T>(a: T, b: T)"),
           _fn("id", "fn id<T>(x: T) -> T"),
           _fn("make", "fn make<T>() -> T")]
    names = lambda ty: [f.name for f in slot_fits(ty, fns) or ()]  # noqa: E731
    assert names("fn(u8, bool)") == []
    assert names("fn(u8) -> bool") == [] and names("fn(u8)") == []
    assert names("fn(u8, u8)") == ["same<T>"]
    assert names("fn(u8) -> u8") == ["id<T>"]
    assert names("fn() -> u16") == ["make<T>"]


def test_a_bounded_parameter_is_listed_with_its_condition() -> None:
    fns = [_fn("show", "fn show<T: Display>(x: T) where T: Debug")]
    (fit,) = slot_fits("fn(u8)", fns) or ()
    assert fit.name == "show<T>" and fit.requires == "u8: Display + Debug"
    table = TypeRecord(file=REL, kind="struct", name="T", fields=(
        FieldRecord(name="f", ty="fn(u8)", fits=(fit,)),))
    assert "      fits: show<T> (only if u8: Display + Debug)" in format_type_for_prompt(
        table, viewer_file=REL)
