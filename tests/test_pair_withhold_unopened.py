"""The measured-owners withholding runs on ONE per-test clock.

`tests/fixtures/route_ledger/pair_rows_stable_2026-09-18.json` is every PAIR
row of run `3127-6763a5c6` (2026-09-18, `anyhow`/`stable`, the first fresh
tree after the pair deferrals shipped: pass 37 of 184, hollow 142): twelve
rows, two pairs through one dispatcher, both deferred `pair_wait` at every
fill from 2 to 7. The withholding read `pair unpaid 0/3` for all 117 of
their leftovers on every iteration, because the unpaid clock counted graded
buys and a deferred pair is never graded.

`tests/fixtures/route_ledger/pair_rows_stable_2026-10-07.json` is every PAIR
row of run `486447-5207101c` (2026-10-07, same crate and cluster): both
formatter pairs deferred at fills 1-4, opened at fill 5 (the display pair
cashed on four OTHER tests, the debug pair reverted), the display pair flat
at fill 6. Under two clocks compared with the grace apart, `test_if` lapsed
at fills 4 and 5 on the unopened clock, the fill-5 open zeroed it, the buy
clock read 2 of 3, and fill 6 withheld again.

The sentence pinned here is the plan's: a fill in which a pair was the
test's candidate and did not turn it green counts, opened or not; the count
resets only on a fill whose pair row paid THAT test. Negative controls: the
buy clock alone (the pre-unopened rule) withholds every 2026-09-18 leftover
at every prefix and re-arms on the 2026-10-07 record at fill 6.
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
    unpaid_pair_buys,
    unpaid_pair_fills,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "route_ledger"
_ORACLE_0918 = "1c3b06644b2e"
_ORACLE_1007 = "cd5c80e3d517"


def _rows(name: str) -> list[RouteLedgerRow]:
    raw = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    out = [RouteLedgerRow.from_dict(r) for r in raw["rows"]]
    return [r for r in out if r is not None]


def _withheld(rows: list[RouteLedgerRow], tests: list[str], oracle: str, *,
              pre_fix: bool) -> frozenset[str]:
    clock = unpaid_pair_buys if pre_fix else unpaid_pair_fills
    return withholding_paired_tests(
        tests, unpaid_fills={t: clock(rows, t, oracle) for t in tests})


def test_the_fixtures_are_the_record() -> None:
    rows = _rows("pair_rows_stable_2026-09-18.json")
    assert len(rows) == 12
    assert {r.kind for r in rows} == {RouteKind.PAIR}
    assert {r.outcome for r in rows} == {RunOutcome.DEFERRED}
    assert {r.iteration for r in rows} == {2, 3, 4, 5, 6, 7}
    assert len(set().union(*(r.target_tests for r in rows))) == 117

    live = _rows("pair_rows_stable_2026-10-07.json")
    assert len(live) == 12
    assert {(r.iteration, r.outcome) for r in live
            if r.outcome is not RunOutcome.DEFERRED} == {
        (5, RunOutcome.CASHED), (5, RunOutcome.REVERTED), (6, RunOutcome.FLAT)}
    cashed = next(r for r in live if r.outcome is RunOutcome.CASHED)
    assert cashed.newly_green is not None
    assert "test_if" not in cashed.newly_green
    assert "test_if" in cashed.target_tests


def test_a_pair_deferred_for_the_grace_stops_withholding() -> None:
    rows = _rows("pair_rows_stable_2026-09-18.json")
    fills = sorted({r.iteration for r in rows})
    for n, it in enumerate(fills, 1):
        prefix = [r for r in rows if r.iteration <= it]
        tests = sorted(set().union(*(r.target_tests for r in prefix)))
        assert _withheld(prefix, tests, _ORACLE_0918, pre_fix=True) == set(tests)
        if n < DEFAULT_UNPAID_GRACE:
            assert _withheld(prefix, tests, _ORACLE_0918,
                             pre_fix=False) == set(tests)
        else:
            assert _withheld(prefix, tests, _ORACLE_0918,
                             pre_fix=False) == frozenset()


def test_an_open_that_paid_another_test_does_not_rearm_the_withholding() -> None:
    """Run `486447-5207101c`, `test_if`: lapsed before fill 4 and stays
    lapsed before fills 5, 6 and 7. The buy clock alone re-arms it before
    fill 6 (2 of 3) — the instance."""
    rows = _rows("pair_rows_stable_2026-10-07.json")
    t = "test_if"
    for fill in (4, 5, 6, 7):
        prefix = [r for r in rows if r.iteration < fill]
        assert _withheld(prefix, [t], _ORACLE_1007, pre_fix=False) == frozenset()
    before_6 = [r for r in rows if r.iteration < 6]
    assert unpaid_pair_buys(before_6, t, _ORACLE_1007) == 2
    assert _withheld(before_6, [t], _ORACLE_1007, pre_fix=True) == {t}
    # Prefix control: before fill 4 the pair had been the candidate for
    # fewer fills than the grace, and the withholding stands.
    before_3 = [r for r in rows if r.iteration < 3]
    assert _withheld(before_3, [t], _ORACLE_1007, pre_fix=False) == {t}


def test_only_a_row_that_paid_the_test_resets_its_clock() -> None:
    rows = _rows("pair_rows_stable_2026-10-07.json")
    cashed = next(r for r in rows if r.outcome is RunOutcome.CASHED)
    paid_test = sorted(cashed.newly_green or ())[0]
    before_5 = [r for r in rows if r.iteration < 5]
    through_5 = [r for r in rows if r.iteration <= 5]
    # The test the cash paid restarts; the sibling it aimed at and did not
    # pay keeps counting.
    assert unpaid_pair_fills(through_5, paid_test, _ORACLE_1007) == 0
    assert unpaid_pair_fills(through_5, "test_if", _ORACLE_1007) == (
        unpaid_pair_fills(before_5, "test_if", _ORACLE_1007) + 1)
    assert unpaid_pair_fills(rows, "test_if", "another-oracle") == 0
    assert unpaid_pair_fills(rows, "test_if", "") == 0


def test_a_stopped_row_neither_counts_nor_interrupts() -> None:
    rows = _rows("pair_rows_stable_2026-09-18.json")
    last = rows[-1]
    t = sorted(last.target_tests)[0]
    stopped = RouteLedgerRow.from_dict(
        {**last.to_dict(), "outcome": RunOutcome.STOPPED.value,
         "iteration": last.iteration + 1})
    assert stopped is not None
    assert (unpaid_pair_fills([*rows, stopped], t, _ORACLE_0918)
            == unpaid_pair_fills(rows, t, _ORACLE_0918) == 6)


def test_the_clock_walks_by_iteration_not_by_insertion() -> None:
    """The ledger appends, so its rows are chronological
    and the insertion walk read right on it. The same record inserted from
    the last fill down to the first, with the pay on fill 2 only, used to
    stop at fill 1, then fill 2, and count 1; the four newer unpaid fills
    (3 to 6) are the count. Control: the chronological record still reads
    6 before fill 7."""
    rows = _rows("pair_rows_stable_2026-10-07.json")
    t = "test_if"
    assert unpaid_pair_fills(rows, t, _ORACLE_1007) == 6
    pay = next(r for r in rows if r.iteration == 2 and t in r.target_tests)
    paid = RouteLedgerRow.from_dict(
        {**pay.to_dict(), "outcome": RunOutcome.CASHED.value,
         "newly_green": [t]})
    assert paid is not None
    with_pay = [paid if r is pay else r for r in rows]
    assert unpaid_pair_fills(with_pay, t, _ORACLE_1007) == 4
    newest_first = sorted(with_pay, key=lambda r: r.iteration, reverse=True)
    assert unpaid_pair_fills(newest_first, t, _ORACLE_1007) == 4
