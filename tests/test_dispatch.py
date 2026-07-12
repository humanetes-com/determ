"""F4 pins — core-first dispatch (defer consumers blocked by stubbed producers)."""
from __future__ import annotations

from pipeline_core.domain.dispatch import DispatchClass, defer_blocked_consumers
from pipeline_core.domain.models import FnLocator

PROD = FnLocator("src/error.rs", "context")   # still-stubbed producer
CONS = FnLocator("src/lib.rs", "wrap")        # filled consumer of PROD


def test_pure_consumer_deferred_producer_active() -> None:
    frontier = {PROD: {"t_len"}, CONS: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert PROD in active and CONS not in active   # producer dispatched
    assert deferred == {CONS: frozenset({PROD})}   # consumer deferred + blocker


def test_consumer_with_unblocked_test_stays_active() -> None:
    # t_own is not owned by any stub → real work the model can do now.
    frontier = {PROD: {"t_len"}, CONS: {"t_len", "t_own"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert CONS in active and not deferred


def test_producer_never_deferred_even_if_blocking_itself() -> None:
    frontier = {PROD: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset({PROD}))
    assert active == frontier and not deferred


def test_readmitted_when_producer_no_longer_stub() -> None:
    # once PROD fills (leaves the stub set), the consumer is active again.
    frontier = {PROD: {"t_len"}, CONS: {"t_len"}}
    active, deferred = defer_blocked_consumers(frontier, frozenset())
    assert CONS in active and not deferred


def test_dispatch_class_values() -> None:
    assert DispatchClass.ACTIVE is not DispatchClass.DEFERRED
