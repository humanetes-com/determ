"""The measured-owners withholding lapses on a pair that never OPENS.

`tests/fixtures/route_ledger/pair_rows_stable_2026-09-18.json` is every PAIR
row of run `3127-6763a5c6` (2026-09-18, `anyhow`/`stable`, the first fresh
tree after the pair deferrals shipped: pass 37 of 184, hollow 142): twelve
rows, two pairs through one dispatcher, both deferred `pair_wait` at every
fill from 2 to 7. The withholding read `pair unpaid 0/3` for all 117 of
their leftovers on every iteration, because the unpaid clock counts graded
buys and a deferred pair is never graded.

The sentence pinned here is the plan's: a pair that has not opened in
`DEFAULT_UNPAID_GRACE` fills in a row stops withholding, by the same bound
as a pair bought and unpaid. Negative control: the same rows through the
pre-fix call (no unopened count), which withholds every leftover at every
prefix. Prefix control: before the third unopened fill the withholding
stands, so the lapse is earned by the record, not granted. Shape control: a
fill in which ANY pair aimed at the test opened resets the count.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.escalation_lifetime import withholding_paired_tests
from pipeline_core.domain.fill_schedule import (
    DEFAULT_UNPAID_GRACE,
    RouteKind,
    RouteLedgerRow,
    RunOutcome,
    unopened_pair_fills,
    unpaid_pair_buys,
)

_FIXTURE = (Path(__file__).parent / "fixtures" / "route_ledger"
            / "pair_rows_stable_2026-09-18.json")
_ORACLE = "1c3b06644b2e"


def _rows() -> list[RouteLedgerRow]:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    out = [RouteLedgerRow.from_dict(r) for r in raw["rows"]]
    return [r for r in out if r is not None]


def _withheld(rows: list[RouteLedgerRow], *, pre_fix: bool) -> frozenset[str]:
    tests = sorted(set().union(*(r.target_tests for r in rows)))
    unpaid = {t: unpaid_pair_buys(rows, t, _ORACLE) for t in tests}
    if pre_fix:
        return withholding_paired_tests(tests, unpaid_buys=unpaid)
    unopened = {t: unopened_pair_fills(rows, t, _ORACLE) for t in tests}
    return withholding_paired_tests(tests, unpaid_buys=unpaid,
                                    unopened_fills=unopened)


def test_the_fixture_is_the_record() -> None:
    rows = _rows()
    assert len(rows) == 12
    assert {r.kind for r in rows} == {RouteKind.PAIR}
    assert {r.outcome for r in rows} == {RunOutcome.DEFERRED}
    assert {r.iteration for r in rows} == {2, 3, 4, 5, 6, 7}
    assert len(set().union(*(r.target_tests for r in rows))) == 117


def test_a_pair_deferred_for_the_grace_stops_withholding() -> None:
    rows = _rows()
    fills = sorted({r.iteration for r in rows})
    for n, it in enumerate(fills, 1):
        prefix = [r for r in rows if r.iteration <= it]
        tests = set().union(*(r.target_tests for r in prefix))
        # Pre-fix: every prefix withholds all of them.
        assert _withheld(prefix, pre_fix=True) == tests
        if n < DEFAULT_UNPAID_GRACE:
            assert _withheld(prefix, pre_fix=False) == tests
        else:
            assert _withheld(prefix, pre_fix=False) == frozenset()


def test_a_fill_that_opened_a_pair_resets_the_count() -> None:
    rows = _rows()
    last = rows[-1]
    opened = RouteLedgerRow.from_dict(
        {**last.to_dict(), "outcome": RunOutcome.FLAT.value})
    assert opened is not None
    t = sorted(last.target_tests)[0]
    assert unopened_pair_fills(rows, t, _ORACLE) == 6
    assert unopened_pair_fills([*rows, opened], t, _ORACLE) == 0
    assert unopened_pair_fills(rows, t, "another-oracle") == 0
    assert unopened_pair_fills(rows, t, "") == 0
