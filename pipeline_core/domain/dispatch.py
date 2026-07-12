"""Core-first dispatch partition (F4, pure domain).

Complements F3's blame partition at the FRONTIER level: a unit whose every
failing test panics inside a DIFFERENT still-`unimplemented!()` producer cannot
be helped by filling it — the test never reaches the unit's own code. Dispatch
it and you burn an LLM call ahead of the producer the test actually blocks on.
:func:`defer_blocked_consumers` moves such pure consumers to DEFERRED; because
it is recomputed each iter from the CURRENT stub set, a consumer is re-admitted
automatically the moment its blocking producer fills (leaves the stub set).
"""

from __future__ import annotations

from enum import Enum, auto

from pipeline_core.domain.models import FnLocator, Frontier


class DispatchClass(Enum):
    ACTIVE = auto()    # fill this iter
    DEFERRED = auto()  # blocked by a still-stubbed producer; re-admitted when it fills


def defer_blocked_consumers(
    frontier: Frontier, stub_keys: frozenset[FnLocator],
) -> tuple[Frontier, dict[FnLocator, frozenset[FnLocator]]]:
    """Split `frontier` into (active, {deferred_unit: blocking_producers}).

    A unit is DEFERRED iff it is NOT itself a stub AND every test attributed to
    it is also attributed to a still-stubbed producer that is NOT this unit —
    i.e. all its failures are blocked externally. Stub units (the producers) are
    always ACTIVE; a unit with an unblocked test stays ACTIVE (it has real work
    the model can do now)."""
    blocked_by: dict[str, set[FnLocator]] = {}
    for k, tests in frontier.items():
        if k in stub_keys:
            for t in tests:
                blocked_by.setdefault(t, set()).add(k)
    active: Frontier = {}
    deferred: dict[FnLocator, frozenset[FnLocator]] = {}
    for key, tests in frontier.items():
        if (key not in stub_keys and tests
                and all(key not in blocked_by.get(t, set()) and t in blocked_by
                        for t in tests)):
            blk: set[FnLocator] = set()
            for t in tests:
                blk |= blocked_by[t]
            deferred[key] = frozenset(blk)
        else:
            active[key] = tests
    return active, deferred
