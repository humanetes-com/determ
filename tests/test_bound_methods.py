"""Unit tests for the bound-provided method inventory.

Pure domain: given the FROZEN where-clause and the TypeRecords it references,
surface the methods a bounded generic makes callable so the body routes through
a bound-provided trait method instead of a mis-bound free constructor (the
context.rs::context E0277 thrash).
"""
from __future__ import annotations

from pipeline_core.domain.bound_methods import (
    format_bound_methods_section,
    select_bound_provider_traits,
)
from pipeline_core.domain.models import MethodRecord, TypeRecord


def _trait(name: str, *methods: tuple[str, str]) -> TypeRecord:
    return TypeRecord(kind="trait", name=name, methods=tuple(
        MethodRecord(name=n, sig=s) for n, s in methods))


def test_selects_only_traits_with_methods():
    bound_trait = _trait("StdError", ("ext_context", "fn ext_context(self) -> Error"))
    marker = _trait("Send")  # no methods (Send/Sync/Sized) → dropped
    struct = TypeRecord(kind="struct", name="Error", methods=(
        MethodRecord(name="new", sig="fn new() -> Error"),))  # not a trait → dropped
    got = select_bound_provider_traits([bound_trait, marker, struct])
    assert [t.name for t in got] == ["StdError"]


def test_format_lists_methods_under_prefer_directive():
    prov = [_trait("StdError", ("ext_context", "fn ext_context<C>(self, c: C) -> Error"))]
    sec = format_bound_methods_section("where E: ext::StdError + Send", prov)
    assert "ext_context" in sec
    assert "StdError" in sec
    assert "where E: ext::StdError + Send" in sec
    # The prefer-over-constructor directive, with GENERIC placeholders.
    assert "PREFER" in sec
    assert "WRONG:" in sec and "RIGHT:" in sec
    # No crate source names baked into the directive examples.
    assert "make_from" in sec and "bound_method" in sec


def test_empty_when_no_provider_traits():
    assert format_bound_methods_section("where E: Send", []) == ""
    # A provider trait whose methods all have empty sigs contributes nothing.
    blank = [_trait("Blank", ("m", ""))]
    assert format_bound_methods_section("where E: Blank", blank) == ""
