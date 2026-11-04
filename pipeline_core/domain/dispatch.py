"""Dispatch partitions that are not the rank (pure domain).

The frontier-level deferral of a unit whose every red test stops in another
stub is `held_stubs.HeldStubs.consumer_deferrals`, read off the sweep's
observed first failures. What stays here: the per-fn fix loop's prediction
for one unit's failed attempt (:func:`sibling_blocked_u2_failures`), the
compile-red narrowing, and which route covers a block.
"""

from __future__ import annotations

from pipeline_core.domain.live_stubs import LiveStubs
from pipeline_core.domain.models import FnLocator, Frontier, ImplBlock


def sibling_blocked_u2_failures(
    failing_tests: frozenset[str], unit: FnLocator,
    dispatch: Frontier, stubs: LiveStubs,
) -> frozenset[FnLocator] | None:
    """The still-stubbed producers holding EVERY test in `failing_tests`, or
    None when at least one failure is this unit's own to fix.

    A PREDICTION from attribution, not an observation, and deliberately not
    the held view (`held_stubs`): the U2 gate has already classified every
    failure whose first panic sits in another unit's stub as external
    (`partition_blame`), so what reaches here failed on its own terms, often
    an assertion, after a body that avoided calling the sibling. The
    question is whether a correct body would need a still-stubbed sibling
    the test's attribution also names. A hub fn is dispatched while the
    rest of its producer chain is still hollow, and its first body then
    fails the per-unit test gate — the
    attributed tests also need the still-stubbed siblings — and that failure
    is attempt-invariant: a correct body panics inside the sibling's
    ``unimplemented!()``, and a body that avoids calling the sibling fails
    the assertion instead. Re-prompting cannot change either outcome, so a
    fix loop entered on this evidence burns its whole budget. Measured on
    the anyhow run `298540-f792975a` (2026-08-12): of a 58-unit per-fn
    batch, exactly 6 units had attributed tests actually RUN at fill time,
    every one failed this way, and the fix loop spent 3.73 call-hours
    re-prompting them (2-13 calls each) against 2.72 call-hours for all 24
    fills that landed.

    Conservative on ignorance, in both directions: a failing test absent
    from `dispatch` is unattributed, and an attributed test held by no
    still-stubbed sibling is the unit's own — either one returns None and
    the fix loop runs; so does an unavailable stub scan.

    `stubs` is the TREE scan, not the frontier: exhaustion-skipped stubs
    leave the frontier but still abort the binary, and they are exactly the
    blockers the recorded deadlock (`259637-36db9aad`) was locked behind. It
    is read here, at the verdict, and not at the start of the pass: a sibling
    kept earlier in the same fill is no longer a stub. On run
    `994565-a5b000bb` (2026-11-04) a pass-start set still held
    `src/error.rs::msg` forty minutes after it was kept, and refused the fix
    loop to `src/error.rs::construct_from_adhoc`, whose own body had crashed
    (`domain/live_stubs.py`)."""
    if not failing_tests:
        return None
    still_stubbed = stubs.keys()
    if still_stubbed is None:
        return None
    blockers: set[FnLocator] = set()
    for t in failing_tests:
        held_by = {k for k, tests in dispatch.items()
                   if t in tests and k != unit and k in still_stubbed}
        if not held_by:
            return None
        blockers |= held_by
    return frozenset(blockers)


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
    Stated once so the impl route's viability filter and the file route's
    unit selection (:func:`file_route_units`) cannot drift — if they drift, a
    stub the impl route silently drops still reads as "covered" to the file
    route and is never filled."""
    return bool(block.container) and block.needs_fill and (
        len(block.stub_members) + len(block.refill_members) <= max_stubs)


def file_route_units(
    blocks: list[ImplBlock], *, max_stubs: int, file_primary: bool = False,
) -> list[tuple[str, list[ImplBlock]]]:
    """The file-first route's dispatch units: ``(file, blocks)`` pairs, one
    prompt per file.

    Default mode is the historical selection, stated here so the impl path
    stops deriving it inline: viable blocks grouped per file, refill-carrying
    blocks the impl route never attempts (free fn / oversized) appended AFTER
    them — appended last so ``containers[0]``, the file's routing target,
    stays an impl header wherever the file has one — and only files holding
    >=2 blocks or at least one such non-viable block qualify. A lone viable
    block is exactly the per-block route's unit, and running it through both
    routes doubles its attempts.

    ``file_primary`` widens the same selection to every block with work,
    including non-viable blocks that carry only stubs (free fns were reachable
    here only as RE-fills before) and single-block files. The per-block loop
    behind it is unchanged — its freshness gate skips whatever the file pass
    completed, so a file-pass failure falls back down the existing ladder."""
    _viable = [b for b in blocks if impl_route_covers(b, max_stubs=max_stubs)]
    if file_primary:
        _extra = [b for b in blocks
                  if not impl_route_covers(b, max_stubs=max_stubs)
                  and b.needs_fill]
    else:
        _extra = [b for b in blocks
                  if not impl_route_covers(b, max_stubs=max_stubs)
                  and b.refill_members]
    by_file: dict[str, list[ImplBlock]] = {}
    for b in (*_viable, *_extra):
        by_file.setdefault(b.file, []).append(b)
    if file_primary:
        return list(by_file.items())
    return [
        (f, bs) for f, bs in by_file.items()
        if len(bs) >= 2 or any(
            not impl_route_covers(b, max_stubs=max_stubs) for b in bs)
    ]


def refills_deferred_by_impl_route(
    blocks: list[ImplBlock], *, max_stubs: int,
) -> frozenset[FnLocator]:
    """Behavioural RE-FILL targets no impl-route unit will attempt — the same
    free-fn and oversized blocks :func:`impl_route_covers` declines,
    read for their `refill_members` instead of their stubs.

    Named because for three days nothing did. A filled free fn implicated by a
    failing test was reachable by no route at all: the impl route declines the
    block on `bool(block.container)` and says the per-fn route will take it, the
    per-fn route is handed the deferred STUBS only, and a filled fn is not a
    stub. On the live anyhow tree that silently excluded all 13 free fns of
    `src/error.rs` — `object_downcast`, `object_drop_front`, `vtable`,
    `context_chain_downcast` and the rest — which is where the crate's
    remaining `heap-use-after-free` lives. Every fill target named in every
    prompt over 2026-08-02/03 was an impl member; not one was a free fn.

    The file-first route now plans these (it is the only route with a
    whole-file view, and the defect they carry is a contract ACROSS several of
    them rather than a fault in any one body). This predicate is what lets a
    caller assert that, and what lets one LOG a target that still ends up
    covered by nothing."""
    return frozenset(
        FnLocator(b.file, m.fn_name)
        for b in blocks if not impl_route_covers(b, max_stubs=max_stubs)
        for m in b.refill_members)
