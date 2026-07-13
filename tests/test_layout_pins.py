"""Pins for the layout-pinning classifier (shared F10/F11 primitive)."""
from __future__ import annotations

from pipeline_core.domain.behaviour import StaticAssertKind
from pipeline_core.domain.layout_pins import (
    classify_layout_assertions,
    is_layout_pinning,
)


def _kinds(src):
    return {o.static_kind for o in classify_layout_assertions(src)}


def test_size_assertion_detected():
    src = "assert_eq!(size_of::<Error>(), size_of::<usize>());"
    assert _kinds(src) == {StaticAssertKind.SIZE}
    assert is_layout_pinning(src)


def test_niche_detected_not_plain_size():
    # NPO: Option<Error> is the same size as Error — a niche pin, not a SIZE pin.
    src = "assert_eq!(size_of::<Result<(), Error>>(), size_of::<Option<Error>>());"
    assert StaticAssertKind.NICHE in _kinds(src)


def test_align_assertion_detected():
    src = "assert_eq!(align_of::<Error>(), align_of::<usize>());"
    assert _kinds(src) == {StaticAssertKind.ALIGN}


def test_autotrait_macro_detected():
    src = "assert_impl_all!(Error: Send, Sync, Unpin, 'static);"
    assert _kinds(src) == {StaticAssertKind.AUTOTRAITS}


def test_where_bound_autotrait_detected():
    src = "fn _assert_send<T: Send + Sync>() {}\n_assert_send::<Error>();"
    assert StaticAssertKind.AUTOTRAITS in _kinds(src)


def test_non_layout_test_is_not_pinning():
    src = 'let e = anyhow!("boom");\nassert_eq!(e.to_string(), "boom");'
    assert not is_layout_pinning(src)
    assert classify_layout_assertions(src) == ()


def test_dedup_repeated_assertions():
    src = ("assert_eq!(size_of::<Error>(), 8);\n"
           "assert_eq!(size_of::<Error>(), 8);")
    assert len(classify_layout_assertions(src)) == 1
