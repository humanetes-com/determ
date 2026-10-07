"""Pins for `domain/escalation_lifetime`: a fired escalation's product lives
exactly as long as the evidence that earned it — no shorter (the fire-once
denominator shrink) and no longer (a product outliving its evidence would be
frontier nobody measured)."""
from __future__ import annotations

from pipeline_core.domain.escalation_lifetime import (
    product_addition_keys,
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


def test_product_addition_keys_are_the_cached_frontier_not_the_earned_set() -> None:
    """Dispatch ranks what sustain merges, not the earned accessors and
    not a `producers_out` side channel that a stub (or an empty persist)
    can leave blank."""
    assert product_addition_keys({"t": _fired()}) == frozenset({_PROD, _STEP})
    assert product_addition_keys({}) == frozenset()


def test_unchanged_evidence_sustains_the_cached_product() -> None:
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC}}, _NO_STUBS)
    assert out.sustained == ("t",)
    assert out.dropped == ()
    assert out.merges == {_PROD: {"t"}, _STEP: {"t"}}


def test_a_changed_unit_set_still_sustains_the_product() -> None:
    """The product is keyed by TEST, not by the accessor set that earned the
    fire. An attribution shift on a still-failing test is the re-buy the
    persisted product exists to close — dropping here would send iter 1
    back to the new accessors."""
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC, _ACC2}}, _NO_STUBS)
    assert out.sustained == ("t",)
    assert out.dropped == ()
    assert out.merges == {_PROD: {"t"}, _STEP: {"t"}}


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


# --- escalation_fire_due: identity is not service under the dispatch cap ----

def _u(name: str) -> FnLocator:
    return FnLocator("src/fmt.rs", name)


def test_fire_needs_every_unit_served_since_the_set_was_observed() -> None:
    """Run `263474-b0e608d3` iter 2: `test_altdebug`'s only unit, `debug`, was
    attributed at iter 1 and parked in batch 3 under a cap of 12. Its set was
    identical at iter 2 — the OLD rule fired and escalated past the body the
    verdict pins. Service, not identity, is the evidence."""
    from pipeline_core.domain.escalation_lifetime import escalation_fire_due
    units = frozenset({_u("debug")})
    # NEGATIVE CONTROL = the old rule: unchanged set alone.
    assert units == units  # the identity the old rule keyed on held
    d = escalation_fire_due(units, units, served_at={}, since=1)
    assert not d.due and d.unserved == (_u("debug"),)
    # Dispatched at iter 3 (its batch): the sweep after it may fire.
    d = escalation_fire_due(units, units, served_at={_u("debug"): 3}, since=1)
    assert d.due and d.unserved == ()
    # Served BEFORE the current set was observed does not count.
    d = escalation_fire_due(units, units, served_at={_u("debug"): 0}, since=1)
    assert not d.due


def test_fire_rule_is_the_old_rule_when_every_unit_is_served() -> None:
    """Cap off: every frontier unit is dispatched every sweep, so an unchanged
    set is exactly the old trigger."""
    from pipeline_core.domain.escalation_lifetime import escalation_fire_due
    units = frozenset({_u("a"), _u("b")})
    served = {_u("a"): 1, _u("b"): 1}
    assert escalation_fire_due(units, units, served, since=1).due
    # A changed set never fires, served or not.
    assert not escalation_fire_due(units, frozenset({_u("a")}), served, 1).due
    assert not escalation_fire_due(units, None, served, 1).due
    # Partial service is no service.
    d = escalation_fire_due(units, units, {_u("a"): 1}, since=1)
    assert not d.due and d.unserved == (_u("b"),)


def test_persisted_futility_seed_counts_as_served() -> None:
    """The floor's `futile_units`, resumed across a code move, seed the set
    at since=0 and every unit at served_at=0: the resumed process fires at
    its FIRST sweep, before its own batch (the `2773540` lock walk)."""
    from pipeline_core.domain.escalation_lifetime import escalation_fire_due
    units = frozenset({_u("downcast")})
    assert escalation_fire_due(units, units, {_u("downcast"): 0}, since=0).due


def test_a_format_trait_test_drops_the_product() -> None:
    """A producer escalation answers "who builds the accessors' container";
    no builder changes what `{:?}` renders, so a fired test whose own source
    formats loses its product — including one resumed from a floor written
    before the fire-side gate existed (run `114436-9cb17382`: a 42-unit
    constructor family re-merged every sweep for two Debug-substring
    tests)."""
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC}}, _NO_STUBS,
        format_tests=frozenset({"t"}))
    assert out.dropped == (("t", "format_trait_test"),)
    assert out.merges == {}


def test_a_non_format_test_is_untouched_by_the_format_gate() -> None:
    """Negative control: the gate keys on the fired test's own source, not on
    the product's shape — a test that formats nothing sustains exactly as
    before the gate existed."""
    out = sustain_escalation_products(
        {"t": _fired()}, {"t": {_ACC}}, _NO_STUBS,
        format_tests=frozenset({"unrelated"}))
    assert out.sustained == ("t",)
    assert out.dropped == ()


def test_a_unit_no_failing_test_executes_does_not_hold_the_fire() -> None:
    """Run `486447-5207101c` iter 7: `test_if`'s set was unchanged, every unit
    in it served except `src/lib.rs::Ok`, which the previous sweep proved no
    failing test executes and which therefore never reaches a batch. The
    gate fires. Negative control: without the measured set it waits on
    `Ok` for ever, exactly as the run logged."""
    from pipeline_core.domain.escalation_lifetime import escalation_fire_due
    from pipeline_core.domain.models import FnLocator

    ok = FnLocator("src/lib.rs", "Ok")
    disp = FnLocator("src/error.rs", "fmt")
    impl = FnLocator("src/fmt.rs", "display")
    units = frozenset({ok, disp, impl})
    served = {disp: 5, impl: 6}
    fired = escalation_fire_due(units, units, served, 4, unexecuted={ok})
    assert fired.due and fired.unserved == ()
    held = escalation_fire_due(units, units, served, 4)
    assert not held.due and held.unserved == (ok,)
    # A unit that is unserved AND executed still holds it.
    still = escalation_fire_due(units, units, {disp: 5}, 4, unexecuted={ok})
    assert not still.due and still.unserved == (impl,)


def test_a_coverage_miss_keeps_the_last_measured_red_dead_set() -> None:
    """The previous sweep classed `Ok` red-dead; this
    sweep read no coverage. The driver stored this sweep's empty set, and
    the next fire held again on `src/lib.rs::Ok`. A miss keeps the last
    measured set; a measurement replaces it, even with empty; the first
    sweep starts empty."""
    from pipeline_core.domain.escalation_lifetime import (
        escalation_fire_due,
        red_dead_after_sweep,
    )
    ok = FnLocator("src/lib.rs", "Ok")
    units = frozenset({ok, _u("render")})
    served = {_u("render"): 2}
    carried = red_dead_after_sweep(frozenset({ok}), None)
    assert carried == {ok}
    assert escalation_fire_due(units, units, served, since=1,
                               unexecuted=carried).due
    # The old store: the miss's empty set holds on `Ok` again.
    held = escalation_fire_due(units, units, served, since=1,
                               unexecuted=frozenset())
    assert not held.due and held.unserved == (ok,)
    assert red_dead_after_sweep(frozenset({ok}), frozenset()) == frozenset()
    assert red_dead_after_sweep(frozenset(), None) == frozenset()
