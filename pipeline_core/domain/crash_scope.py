"""While a test dies of a native crash, only the crash's culprits can move it
(pure domain).

A test whose binary aborts on a memory fault or a stack overflow never reaches
its assertion. Its static owners — the units its source names, the format
implementer its `{:?}` resolves to, the coverage owners it entered before the
abort — are dispatched on it all the same, and every one of those rewrites is
judged against a verdict no body except the crash's culprit can change. Run
`371055-1a407b3c` (2026-09-27): session #0 held 26 units for 7 aborted tests
and the per-fn wave re-filled `fmt.rs::display` for "13 failing tests", every
one an abort that never reached `display`.

The rule: while the sweep's localizer names culprits C for a failing test,
that test's movable owners are C. A frontier unit outside C stops carrying the
test (it is recorded as blocked by the crash, not dispatched on it); a unit
left carrying no test leaves the frontier; C stays carrying the test. When
the localizer names no culprit, nothing changes — an unattributed crash is not
evidence about which body cannot move the test.

Generic: the inputs are the localizer's sites and the frontier's own map; the
rule names no test, unit or crate.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.oracle_ledger import bare_test_name

__all__ = ["CrashScope", "crash_culprits_by_test", "scope_to_crash_culprits"]


@dataclass(frozen=True, slots=True)
class CrashScope:
    """The frontier after scoping, and what the scoping took off it."""

    ordered: tuple[tuple[FnLocator, set[str]], ...]
    #: Test -> units it was removed from (blocked by its crash).
    blocked: Mapping[str, tuple[FnLocator, ...]]
    #: Units that left the frontier because their only tests were crashes
    #: they are not culprits of.
    dropped: tuple[FnLocator, ...]


def crash_culprits_by_test(
    sites: Iterable[object], failing: Iterable[str],
) -> dict[str, frozenset[FnLocator]]:
    """Bare failing-test name -> the culprit locators every localized site
    of that test names (union over sites). A site with no test name or no
    culprit contributes nothing."""
    reds = {bare_test_name(t) for t in failing}
    out: dict[str, set[FnLocator]] = {}
    for site in sites:
        test = str(getattr(site, "test_name", "") or "")
        culprits = frozenset(getattr(site, "culprit_locators", ()) or ())
        if not test or not culprits:
            continue
        bare = bare_test_name(test)
        if bare in reds:
            out.setdefault(bare, set()).update(culprits)
    return {t: frozenset(c) for t, c in out.items()}


def scope_to_crash_culprits(
    ordered: Sequence[tuple[FnLocator, set[str]]],
    culprits_by_test: Mapping[str, frozenset[FnLocator]],
    exempt: Collection[FnLocator] = (),
) -> CrashScope:
    """`ordered` with every crashed test removed from the units that are not
    its culprits; order is kept. An `exempt` unit (a still-stubbed unit a red
    test's first failure sits in, `held_stubs`) keeps its tests: whatever the
    crash, that test cannot get past the stub until it has a body."""
    if not culprits_by_test:
        return CrashScope(tuple(ordered), {}, ())
    exempt_set = frozenset(exempt)
    out: list[tuple[FnLocator, set[str]]] = []
    blocked: dict[str, list[FnLocator]] = {}
    dropped: list[FnLocator] = []
    for loc, tests in ordered:
        if loc in exempt_set:
            out.append((loc, tests))
            continue
        keep: set[str] = set()
        for t in tests:
            culprits = culprits_by_test.get(bare_test_name(t))
            if culprits is not None and loc not in culprits:
                blocked.setdefault(bare_test_name(t), []).append(loc)
            else:
                keep.add(t)
        if keep or not tests:
            out.append((loc, keep if tests else tests))
        else:
            dropped.append(loc)
    return CrashScope(tuple(out),
                      {t: tuple(ls) for t, ls in sorted(blocked.items())},
                      tuple(dropped))
