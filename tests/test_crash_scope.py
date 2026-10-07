"""A crashed test is dispatched only on its crash's culprits.

Shape of run `486447-5207101c` iter 6 with the allocation-origin read: the
`tests/test_downcast.rs::test_drop` heap-use-after-free is blamed on
`Error::construct_from_std`; the test's static owners also include `downcast`
and `vtable`. Negative control: a test with no localized culprit keeps every
owner.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline_core.domain.crash_scope import (
    crash_culprits_by_test,
    scope_to_crash_culprits,
)
from pipeline_core.domain.models import FnLocator

_STD = FnLocator("src/error.rs", "construct_from_std")
_DOWN = FnLocator("src/error.rs", "downcast")
_VT = FnLocator("src/error.rs", "vtable")


def test_the_crashed_test_stays_only_on_its_culprit() -> None:
    sites = [SimpleNamespace(test_name="test_drop", culprit_locators={_STD}),
             SimpleNamespace(test_name="", culprit_locators={_VT})]
    culprits = crash_culprits_by_test(
        sites, ["tests/test_downcast.rs::test_drop", "test_downcast"])
    assert culprits == {"test_drop": frozenset({_STD})}
    ordered = [(_DOWN, {"test_drop", "test_downcast"}),
               (_VT, {"test_drop"}),
               (_STD, {"test_drop"})]
    scope = scope_to_crash_culprits(ordered, culprits)
    assert scope.ordered == ((_DOWN, {"test_downcast"}), (_STD, {"test_drop"}))
    assert scope.blocked == {"test_drop": (_DOWN, _VT)}
    assert scope.dropped == (_VT,)


def test_no_culprit_no_scoping() -> None:
    ordered = [(_DOWN, {"test_drop"}), (_VT, {"test_drop"})]
    unattributed = [SimpleNamespace(test_name="test_drop", culprit_locators=())]
    scope = scope_to_crash_culprits(
        ordered, crash_culprits_by_test(unattributed, ["test_drop"]))
    assert scope.ordered == tuple(ordered) and not scope.blocked
    # A crash of a test that is no longer failing scopes nothing either.
    passing = [SimpleNamespace(test_name="test_drop", culprit_locators={_STD})]
    assert crash_culprits_by_test(passing, ["test_other"]) == {}
