"""The recorded executed set of a test — what the retrospectives store says a
test entered when it last ran to a verdict.

A behavioural retrospective is stamped, at conviction time, with the bodies
each convicting test measurably entered (`retro_gate.CONVICTOR_ENTRY`, a
mapping test name → sorted `file::fn` strings, written from the sweep's
bound coverage reading). That stamp is the only record the pipeline keeps of
a test's executed path across sweeps, and it is the evidence the coverage
reading has to fall back on for a test whose process no longer completes: a
binary killed by a signal flushes no profile, so the live reading knows
nothing about that test, while the store still says what it entered the
last time it produced a verdict.

Measured 2026-09-07 on `tmp/anyhow/.phase_c_logs/retrospectives.json`
(run `1577437-c93d5c26`): the SIGSEGV test
`test_reallocate_into_boxed_dyn_error_without_backtrace_preserves_std_error_trait`
is stamped on five records, with entry sets of 2, 3, 8, 8 and 8 units; the
8-unit stamps carry the crash's culprit
(`src/error.rs::reallocate_into_boxed_dyn_error_without_backtrace`) which
the live reading had classed dead.

Records carry no stamp time, so "the last recorded set" is not recoverable
per record; the reader returns the UNION over every record's stamp for the
test. Each stamp is positive evidence ("the test entered these bodies at
some verdict"), and every consumer of an owner set refuses on positive
evidence only, so the disjunction of readings is the set for which "no
recorded reading shows this test entering the unit" holds.

Generic: any store, any test name, any crate; no test or unit name is an
input. Pure over a loaded mapping — the adapter that reads the file is
`pipeline_core.adapters.recorded_entry`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_core.domain.coverage_slice import matching_test_key
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import CONVICTOR_ENTRY

__all__ = ["recorded_entry_sets", "stamped_entry_sets"]


def _locator(text: str) -> FnLocator | None:
    """`src/error.rs::construct` → `FnLocator`; the stamp is
    `f"{file}::{fn_name}"` and neither half carries `::`."""
    if "::" not in text:
        return None
    file, fn = text.rsplit("::", 1)
    if not file or not fn:
        return None
    return FnLocator(file, fn)


def stamped_entry_sets(
    store: Mapping[str, Any],
) -> dict[str, frozenset[FnLocator]]:
    """Every test the store has an entry stamp for, keyed as stamped (the
    convicting test's name as the retrospective recorded it), with the
    union of its stamped units across records. Non-record values (the
    store's `__migrations__` list) and malformed stamps are skipped."""
    out: dict[str, set[FnLocator]] = {}
    for rec in store.values():
        if not isinstance(rec, Mapping):
            continue
        entry = rec.get(CONVICTOR_ENTRY)
        if not isinstance(entry, Mapping):
            continue
        for test, units in entry.items():
            if not isinstance(units, list):
                continue
            bucket = out.setdefault(str(test), set())
            for u in units:
                loc = _locator(str(u))
                if loc is not None:
                    bucket.add(loc)
    return {t: frozenset(us) for t, us in out.items() if us}


def recorded_entry_sets(
    store: Mapping[str, Any],
    tests: Iterable[str],
) -> dict[str, frozenset[FnLocator]]:
    """The recorded executed set for each of `tests`, keyed by the name AS
    ASKED. A test with no stamp anywhere in the store is absent — the
    caller's ignorance case, never an empty set. Name reconciliation is
    exact-then-unique-leaf (`matching_test_key`), the same rule the live
    owner map applies between the sweep's qualified form and a bare leaf."""
    stamped = stamped_entry_sets(store)
    out: dict[str, frozenset[FnLocator]] = {}
    for name in dict.fromkeys(tests):
        key = matching_test_key(name, stamped)
        if key is None:
            continue
        out[name] = stamped[key]
    return out
