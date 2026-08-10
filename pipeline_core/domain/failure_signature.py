"""Structural failures read off failing-test sets, before more budget burns.

Two observations, both from the same 2026-08-10 log audit, motivate treating
an identical failing-test set as a verdict about the TREE rather than about
any one attempt:

- within one unit: `src/error.rs::ErrorImpl` failed with the exact
  `7 failing test(s)` signature through THREE consecutive runs — 10 attempts,
  19 maximal last-resort prompts life-to-date, zero acceptances. Every
  attempt after the first purchased information the first had already paid
  for.
- across units: three sibling `From<Error>` impl blocks each failed U2 on
  the same single test (`test_no_backtrace_fallback`) in the same pass, in
  two consecutive runs — a test whose subject fn none of the three units
  contains. The failure was a property of the shared tree, and each unit
  was charged for it separately.

A capped mechanism failing with an attempt-invariant reason is RED at
attempt 1 — retrying is the same attempt again. These helpers are pure so
the fix loops can consult them mid-episode and the walk suite can replay
the decisions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def attempt_invariant(failing_sets: Sequence[frozenset[str]]) -> bool:
    """True when the last two judged attempts failed on an IDENTICAL,
    non-empty failing-test set.

    Only consecutive attempts are compared: an episode that oscillates
    between two signatures is making (bad) progress of a different kind and
    stays eligible for its remaining budget. Empty sets never match — a
    compile failure reports no failing tests, and equating two of those
    would re-invent the compile-error signature check that already exists
    at the fix loops."""
    if len(failing_sets) < 2:
        return False
    prev, cur = failing_sets[-2], failing_sets[-1]
    return bool(cur) and prev == cur


def shared_failure_groups[KeyT](
    failing_by_unit: Mapping[KeyT, frozenset[str]], *, min_units: int = 3,
) -> dict[frozenset[str], frozenset[KeyT]]:
    """Units of one pass that failed on the SAME non-empty failing-test set,
    grouped by that set, keeping only groups of at least `min_units`.

    A failing set shared by that many units in one pass implicates something
    they share — a producer, a layout contract — not any of them. The caller
    stops the fix loop for the whole group and surfaces the shared set to
    attribution instead of charging each member."""
    by_set: dict[frozenset[str], set[KeyT]] = {}
    for unit, failing in failing_by_unit.items():
        if failing:
            by_set.setdefault(failing, set()).add(unit)
    return {
        failing: frozenset(units)
        for failing, units in by_set.items() if len(units) >= min_units
    }


def structural_group_note(
    failing: frozenset[str], units: frozenset[object],
) -> str:
    """One log line naming the group and the shared set, for the breaker."""
    tests = ", ".join(sorted(failing)[:4])
    more = len(failing) - min(len(failing), 4)
    suffix = f" (+{more} more)" if more else ""
    return (f"{len(units)} unit(s) share the identical failing set "
            f"[{tests}{suffix}] — structural: the failure implicates what "
            "they share, not any member; stopping the group's fix loops "
            "and handing the set to attribution.")
