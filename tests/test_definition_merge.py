"""Pins for body preservation across a declaration reinstall.

The incident: `layout-sync` reinstalled two of `kind.rs`'s three `*Kind` trait
definitions at every startup. A trait declaration holds its default method
bodies, so the reinstall re-emitted `anyhow_kind` as a one-line stub and threw
away a body the fill loop had just landed — roughly a dozen times over nine
days, each round a full model call for a five-character answer."""

from __future__ import annotations

from pipeline_core.application.definition_merge import merge_live_bodies

_AUTHORED = (
    "pub trait AdhocKind: Sized {\n"
    "    #[inline]\n"
    "    fn anyhow_kind(&self) -> Adhoc { unimplemented!() }\n"
    "}"
)
_LIVE = (
    "pub trait AdhocKind: Sized {\n"
    "    #[inline]\n"
    "    fn anyhow_kind(&self) -> Adhoc { Adhoc }\n"
    "}"
)


def test_a_filled_default_body_survives_the_reinstall() -> None:
    assert merge_live_bodies(_AUTHORED, _LIVE) == _LIVE


def test_the_authored_structure_still_wins() -> None:
    # Bodies come from live; everything else is the authored declaration's.
    authored = (
        "pub trait AdhocKind: Sized + Debug {\n"
        "    fn anyhow_kind(&self) -> Adhoc { unimplemented!() }\n"
        "}"
    )
    out = merge_live_bodies(authored, _LIVE)
    assert "Sized + Debug" in out and "{ Adhoc }" in out
    assert "unimplemented!()" not in out


def test_a_stub_in_the_live_tree_does_not_overwrite_an_authored_body() -> None:
    # Live is the older, unfilled state here; the reinstall must not import it.
    assert merge_live_bodies(_LIVE, _AUTHORED) == _LIVE


def test_members_absent_from_either_side_are_untouched() -> None:
    authored = ("pub trait K {\n"
                "    fn a(&self) -> u8 { unimplemented!() }\n"
                "    fn b(&self) -> u8 { unimplemented!() }\n"
                "}")
    live = "pub trait K {\n    fn a(&self) -> u8 { 7 }\n}"
    out = merge_live_bodies(authored, live)
    assert "fn a(&self) -> u8 { 7 }" in out
    assert "fn b(&self) -> u8 { unimplemented!() }" in out


def test_several_members_all_shift_correctly() -> None:
    # Spliced right-to-left; a left-to-right pass would corrupt later spans
    # once an earlier body changed length.
    authored = ("pub trait K {\n"
                "    fn a(&self) -> u8 { unimplemented!() }\n"
                "    fn b(&self) -> u8 { unimplemented!() }\n"
                "    fn c(&self) -> u8 { unimplemented!() }\n"
                "}")
    live = ("pub trait K {\n"
            "    fn a(&self) -> u8 { 1 + 1 + 1 + 1 }\n"
            "    fn b(&self) -> u8 { 2 }\n"
            "    fn c(&self) -> u8 { 3 }\n"
            "}")
    out = merge_live_bodies(authored, live)
    assert "unimplemented!()" not in out
    assert "{ 1 + 1 + 1 + 1 }" in out and "{ 2 }" in out and "{ 3 }" in out


def test_a_nested_brace_in_a_body_does_not_end_it_early() -> None:
    authored = "pub trait K {\n    fn a(&self) -> u8 { unimplemented!() }\n}"
    live = ("pub trait K {\n"
            "    fn a(&self) -> u8 { if x { 1 } else { 2 } }\n"
            "}")
    assert "if x { 1 } else { 2 }" in merge_live_bodies(authored, live)


def test_no_members_means_the_authored_text_is_returned_unchanged() -> None:
    assert merge_live_bodies("struct S { a: u8 }", "struct S { b: u8 }") == \
        "struct S { a: u8 }"
