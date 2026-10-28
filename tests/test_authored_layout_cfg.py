"""A re-authored item keeps the cfg gate the crate declares for it.

Run `42057-641e589c` (2026-10-26): the authored `Backtrace` enum dropped the
frozen `#[cfg(not(feature = "std"))]`, so under `std` the skeleton held both
the enum and `pub(crate) use std::backtrace::Backtrace`, and every attempt
of the component failed E0255. Claims: the reply's outer `#[cfg]` lines are
the frozen ones (added, replaced, or removed), for every item kind; a cfg in
the body and a `cfg_attr` are left; the carried text is what the splice and
the registry store (`carry_frozen_facts`). Negative control: the carry the
store applied before this change (repr, derives, visibility) keeps no cfg.
Invented shapes.
"""
from __future__ import annotations

from pipeline_core.domain.authored_layout import (
    carry_frozen_cfg,
    carry_frozen_derives,
    carry_frozen_repr,
)

_FROZEN = '#[cfg(not(feature = "std"))]\npub(crate) enum Trace {\n    Off,\n}'


def test_a_dropped_gate_is_carried_back() -> None:
    reply = "pub(crate) enum Trace {\n    Off,\n    On,\n}"
    assert carry_frozen_cfg(_FROZEN, reply) == (
        '#[cfg(not(feature = "std"))]\npub(crate) enum Trace {\n    Off,\n    On,\n}')
    # Control: the carry before this change leaves the reply ungated.
    before = carry_frozen_derives(_FROZEN, carry_frozen_repr(_FROZEN, reply))
    assert "cfg" not in before


def test_a_different_gate_is_replaced_and_one_the_crate_lacks_is_removed() -> None:
    reply = '#[cfg(feature = "std")]\n#[derive(Clone)]\npub(crate) enum Trace {\n    Off,\n}'
    out = carry_frozen_cfg(_FROZEN, reply)
    assert out.count("#[cfg(") == 1 and '#[cfg(not(feature = "std"))]' in out
    assert "#[derive(Clone)]" in out
    ungated = "pub(crate) fn make() -> u8 {\n    0\n}"
    assert carry_frozen_cfg(ungated, '#[cfg(test)]\n' + ungated) == ungated


def test_every_item_kind_and_a_multiline_gate() -> None:
    frozen = '#[cfg(any(\n    feature = "a",\n    feature = "b",\n))]\n    fn go(&self) -> u8;'
    reply = "    fn go(&self) -> u8;"
    out = carry_frozen_cfg(frozen, reply)
    assert out.splitlines()[0].strip().startswith("#[cfg(any(")
    assert out.endswith("    fn go(&self) -> u8;")
    alias = carry_frozen_cfg("#[cfg(unix)]\ntype Fd = i32;", "type Fd = i64;")
    assert alias == "#[cfg(unix)]\ntype Fd = i64;"


def test_body_cfgs_and_cfg_attr_are_the_reply_s() -> None:
    reply = ('#[cfg_attr(test, derive(Debug))]\npub(crate) enum Trace {\n'
             '    #[cfg(feature = "std")]\n    On,\n}')
    out = carry_frozen_cfg(_FROZEN, reply)
    assert '#[cfg_attr(test, derive(Debug))]' in out
    assert '    #[cfg(feature = "std")]\n    On,' in out
    assert out.count('#[cfg(not(feature = "std"))]') == 1


def test_an_agreeing_gate_returns_the_reply_unchanged() -> None:
    reply = '#[cfg(not( feature = "std" ))]\n/// doc\npub(crate) enum Trace { Off }'
    assert carry_frozen_cfg(_FROZEN, reply) == reply


def test_no_item_or_an_unclosed_attribute_is_returned_as_written() -> None:
    from pipeline_core.domain.authored_layout import _attribute_prefix
    assert _attribute_prefix("// only a comment\n") is None
    assert _attribute_prefix("#[cfg(any(\n    unix,\n") is None
    assert carry_frozen_cfg(_FROZEN, "// nothing here") == "// nothing here"
    assert carry_frozen_cfg("#[cfg(unix)\n", "fn f() {}") == "fn f() {}"
