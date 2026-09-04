"""The stub inventory carries sites and units by name — the type-level fix for
a bare list that repeated a locator once per impl site.

Plan sentence: a consumer that wants a count or a sequence names the
cardinality; nothing about the type lets it iterate sites as if they were
units. Negative control: the pre-fix shape (a list with repeats) is what
`of_units` is deliberately unable to reproduce.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.stub_inventory import StubInventory, StubSite

_RECS: list[dict[str, Any]] = [
    {"file": "src/error.rs", "fn_name": "from", "container": "impl From<A> for E",
     "cfg_predicates": []},
    {"file": "src/error.rs", "fn_name": "from", "container": "impl From<B> for E",
     "cfg_predicates": []},
    {"file": "src/error.rs", "fn_name": "provide", "container": "impl Error for E",
     "cfg_predicates": ["error_generic_member_access"]},
    {"file": "src/ptr.rs", "fn_name": "new", "container": "impl Own<T>",
     "cfg_predicates": []},
]


def test_both_cardinalities_are_named_and_neither_is_the_default() -> None:
    inv = StubInventory.from_records(_RECS)
    assert inv.site_count == 4 and inv.unit_count == 3
    assert inv.units == {FnLocator("src/error.rs", "from"),
                         FnLocator("src/error.rs", "provide"),
                         FnLocator("src/ptr.rs", "new")}
    # first-seen order, one entry per unit
    assert inv.unit_list() == [FnLocator("src/error.rs", "from"),
                               FnLocator("src/error.rs", "provide"),
                               FnLocator("src/ptr.rs", "new")]
    assert len(inv.by_unit[FnLocator("src/error.rs", "from")]) == 2
    assert inv.render_count() == "4 stub site(s) in 3 unit(s)"
    assert not hasattr(inv, "__len__") and not hasattr(inv, "__iter__")


def test_site_pairs_is_the_explicit_per_site_view() -> None:
    inv = StubInventory.from_records(_RECS)
    pairs = inv.site_pairs()
    assert len(pairs) == 4
    assert pairs[2] == (FnLocator("src/error.rs", "provide"),
                        ("error_generic_member_access",))


def test_of_units_collapses_repeats_and_cannot_invent_sites() -> None:
    """A caller holding a multiset has already lost the sites; the constructor
    refuses to pretend otherwise."""
    inv = StubInventory.of_units([FnLocator("a.rs", "f"), ("a.rs", "f"),
                                  FnLocator("b.rs", "g")])
    assert (inv.site_count, inv.unit_count) == (2, 2)
    assert all(s.site.container == "" for s in inv.sites)
    assert inv.render_count() == "2 stub(s)"


def test_membership_reads_locators_sites_and_tuples() -> None:
    inv = StubInventory.from_records(_RECS)
    assert FnLocator("src/ptr.rs", "new") in inv
    assert ("src/ptr.rs", "new") in inv
    assert FnSite("src/error.rs", "from", "impl From<B> for E") in inv
    assert FnSite("src/error.rs", "from", "impl From<Z> for E") not in inv
    assert ("src/ptr.rs", "missing") not in inv


def test_filter_keeps_the_type_and_the_order() -> None:
    inv = StubInventory.from_records(_RECS)
    kept = inv.filter(lambda s: s.site.file == "src/error.rs")
    assert isinstance(kept, StubInventory)
    assert kept.site_count == 3 and kept.unit_count == 2
    assert StubSite(FnSite("src/ptr.rs", "new", "impl Own<T>"), ()) not in kept


def test_unknown_cfg_is_none_not_empty() -> None:
    inv = StubInventory.of_units([("a.rs", "f")], cfg_predicates=None)
    assert inv.site_pairs() == ((FnLocator("a.rs", "f"), None),)
    inv2 = StubInventory.from_records([{"file": "a.rs", "fn_name": "f"}])
    assert inv2.site_pairs() == ((FnLocator("a.rs", "f"), ()),)
