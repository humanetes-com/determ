"""Pins for `domain/escalation_lifetime`: a fired escalation's product lives
exactly as long as the evidence that earned it — no shorter (the fire-once
denominator shrink) and no longer (a product outliving its evidence would be
frontier nobody measured)."""
from __future__ import annotations

from pipeline_core.domain.escalation_lifetime import (
    sustain_escalation_products,
)
from pipeline_core.domain.models import FnLocator

_ACC = FnLocator("src/a.rs", "accessor")
_ACC2 = FnLocator("src/a.rs", "other_accessor")
_PROD = FnLocator("src/b.rs", "producer")
_STEP = FnLocator("src/c.rs", "source")

_NO_STUBS: frozenset[FnLocator] = frozenset()


def _fired(*, earned=frozenset({_ACC}), cached=None):
    return (earned, cached if cached is not None
            else {_PROD: {"t"}, _STEP: {"t"}})


def test_unchanged_evidence_sustains_the_cached_product() -> None:
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC}}, _NO_STUBS)
    assert out.sustained == ("t",)
    assert out.dropped == ()
    assert out.merges == {_PROD: {"t"}, _STEP: {"t"}}


def test_a_changed_unit_set_drops_the_product() -> None:
    """The attribution moved: the set that earned the fire no longer
    describes the test, so nothing measured supports the additions."""
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC, _ACC2}}, _NO_STUBS)
    assert out.sustained == ()
    assert out.dropped == (("t", "evidence_changed"),)
    assert out.merges == {}


def test_an_accessor_that_is_a_stub_again_drops_the_product() -> None:
    """A re-stubbed accessor was never served by any rewrite, so 'futile'
    no longer describes it — same rule that withholds the fire itself."""
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC}}, frozenset({_ACC}))
    assert out.dropped == (("t", "stub_accessor"),)
    assert out.merges == {}


def test_a_test_that_stopped_failing_drops_the_product() -> None:
    """No failing test, no attribution, no evidence: the product may not
    keep dead frontier alive after the test it served has passed."""
    out = sustain_escalation_products({"t": _fired()}, {}, _NO_STUBS)
    assert out.dropped == (("t", "not_failing"),)
    assert out.merges == {}


def test_merges_union_across_sustained_tests_and_products_are_untouched():
    """Two sustained tests naming one producer merge into one frontier entry
    carrying both tests; the input mapping itself is not mutated — the
    caller owns the deletes."""
    products = {"t1": (frozenset({_ACC}), {_PROD: {"t1"}}),
                "t2": (frozenset({_ACC2}), {_PROD: {"t2"}}),
                "t3": (frozenset({_ACC}), {_STEP: {"t3"}})}
    out = sustain_escalation_products(
        products, {"t1": {_ACC}, "t2": {_ACC2}}, _NO_STUBS)
    assert out.sustained == ("t1", "t2")
    assert out.dropped == (("t3", "not_failing"),)
    assert out.merges == {_PROD: {"t1", "t2"}}
    assert set(products) == {"t1", "t2", "t3"}, "input mapping not mutated"
