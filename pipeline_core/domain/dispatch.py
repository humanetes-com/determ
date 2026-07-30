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

from pipeline_core.domain.models import FnLocator, Frontier, ImplBlock


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


def restrict_to_compile_blockers(
    frontier: Frontier, implicated: frozenset[FnLocator],
) -> tuple[Frontier, frozenset[FnLocator]]:
    """Split `frontier` into (actionable, withheld) for a crate that does NOT
    COMPILE — where the only fn worth an LLM call is one the compiler names.

    While the build is red no test reports a verdict, so every behavioural
    signal the attribution chain runs on is absent and the frontier it returns
    is reachability, not evidence. Dispatching from it spends the budget on fns
    that are fine: measured live (`anyhow`, cluster `nightly`), a 39-error tree
    produced a 72-fn frontier of which NONE were implicated, and all 72 came
    back `signal=downstream_only` — 354 model calls over 8.7 hours that could
    not have moved the score whatever they wrote, because the errors were always
    somewhere else.

    Implicated fns the frontier does not already carry are ADDED, with an empty
    test set: no test can name them while the crate is red, and their being
    unnamed is the condition, not a reason to skip them. Because they are ADDED
    they arrive behind whatever filters the caller already applied to the
    frontier — the test-oracle filter above all, since a red build's diagnostics
    point at the test binaries — so the caller must re-apply them to the result.

    An empty `implicated` returns an empty frontier. That is the honest answer
    for "the compiler blames nothing this tree can fill", but it is ALSO what a
    blind attribution returns: the text scan attributes a macro-expansion error
    to nothing at all. Read one empty result as "no aim this sweep", never as a
    verdict on the cluster — a caller that exits on it exits on the attribution's
    blind spot as readily as on the real thing."""
    actionable: Frontier = {
        key: tests for key, tests in frontier.items() if key in implicated}
    for key in implicated:
        actionable.setdefault(key, set())
    withheld = frozenset(frontier) - frozenset(actionable)
    return actionable, withheld


def impl_route_covers(block: ImplBlock, *, max_stubs: int) -> bool:
    """True when the per-impl fill route will attempt `block` at all.

    A free-fn container (`""`) and a block whose unit size exceeds the per-impl
    cap are both delegated to the per-fn route; a block with no work is skipped.
    Stated once so the impl route's viability filter and the engine's route
    SEQUENCING check cannot drift — if they drift, a stub the impl route
    silently drops still reads as "covered" to the sequencer and is never
    filled."""
    return bool(block.container) and block.needs_fill and (
        len(block.stub_members) + len(block.refill_members) <= max_stubs)


def stubs_deferred_by_impl_route(
    blocks: list[ImplBlock], *, max_stubs: int,
) -> frozenset[FnLocator]:
    """Stubs that no impl-route unit will attempt — free fns and members of
    oversized blocks.

    These must be filled BEFORE any impl-route behavioural re-fill, not after
    it. An `unimplemented!()` reached from a drop glue / vtable slot panics and
    ABORTS the whole test binary, so every re-fill measured against that binary
    is judged "crashed the siblings" and reverted however good the body was —
    the re-fill work is unwinnable until the stub is gone. Running the impl
    route first therefore spends the LLM budget on guaranteed reverts and
    schedules the one fill that would unblock them last."""
    return frozenset(
        FnLocator(b.file, m.fn_name)
        for b in blocks if not impl_route_covers(b, max_stubs=max_stubs)
        for m in b.stub_members)
