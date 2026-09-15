"""The measured-owners withholding on the RECORDED route ledger.

`tests/fixtures/route_ledger/pair_rows_stable_2026-09-15.json` is every PAIR
row the live `anyhow`/`stable` ledger held on 2026-09-15, in file order (the
ledger is append-only, so file order is time): 68 rows, 54 `reverted` and 14
`flat`, none cashed, spanning runs `200802-b835274e` through
`1158170-ce307ee7`. The last of those runs measured the executed slice of its
three leftovers at 29 units at iteration 1 and dispatched 3, at `pass=122
fail=3` both times, and was stopped by hand at the plateau.

The sentence pinned here is the PLAN's, not the implementation's: on this
record all three leftovers have a hop pair that has been bought and returned
nothing far past the bound the schedule already lapses a route's exemptions
on, so all three stop withholding their coverage owners. The negative control
is the same rows replayed as a prefix — as of the pair's first and second
graded buy the withholding stands, because the lapse has to be EARNED by the
pair's own failures and not granted at the first try. The oracle control is
the same rows asked about under a different fingerprint.

`scripts/pair_withhold_fixture.py` computes the same thing against the live
files rather than this snapshot.
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
)

_FIXTURE = (Path(__file__).parent / "fixtures" / "route_ledger"
            / "pair_rows_stable_2026-09-15.json")
_ORACLE = "e4957ec70ac8"
_LEFTOVERS = (
    "test_altdebug",
    "test_debug_backtrace_captured_simple",
    "test_debug_backtrace_captured_with_cause",
)


def _rows() -> list[RouteLedgerRow]:
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    out = [RouteLedgerRow.from_dict(r) for r in raw["rows"]]
    return [r for r in out if r is not None]


def test_the_fixture_is_the_record() -> None:
    """The check that this is the ledger and not a hand-made list: every row
    is a PAIR run under the one oracle, none cashed, and the newest is the
    run that was stopped at the plateau."""
    rows = _rows()
    assert len(rows) == 68
    assert {r.kind for r in rows} == {RouteKind.PAIR}
    assert {r.oracle_fp for r in rows} == {_ORACLE}
    assert not [r for r in rows
                if r.outcome is RunOutcome.CASHED or r.net_pass_delta > 0]
    assert rows[-1].run_id == "1158170-ce307ee7"
    assert all(set(_LEFTOVERS) <= set(r.target_tests) for r in rows)


def test_every_recorded_leftover_lapses_its_withholding() -> None:
    """The plan's sentence. 68 graded pair buys, none paying, against a
    bound of 3: the coverage owners those tests execute through are
    admissible."""
    rows = _rows()
    unpaid = {t: unpaid_pair_buys(rows, t, _ORACLE) for t in _LEFTOVERS}
    assert unpaid == dict.fromkeys(_LEFTOVERS, 68)
    assert withholding_paired_tests(
        _LEFTOVERS, unpaid_buys=unpaid) == frozenset()


def test_the_lapse_is_earned_not_granted() -> None:
    """NEGATIVE CONTROL, on the same rows: replayed as a prefix, the
    withholding stands for every buy before the bound. A clause that
    released at the first buy would be the blind re-buy, not a lapse."""
    rows = _rows()
    for n in range(1, DEFAULT_UNPAID_GRACE):
        prefix = rows[:n]
        unpaid = {t: unpaid_pair_buys(prefix, t, _ORACLE) for t in _LEFTOVERS}
        assert set(unpaid.values()) == {n}
        assert withholding_paired_tests(
            _LEFTOVERS, unpaid_buys=unpaid) == frozenset(_LEFTOVERS)
    at_bound = rows[:DEFAULT_UNPAID_GRACE]
    unpaid = {t: unpaid_pair_buys(at_bound, t, _ORACLE) for t in _LEFTOVERS}
    assert withholding_paired_tests(
        _LEFTOVERS, unpaid_buys=unpaid) == frozenset()


def test_rows_under_another_oracle_are_not_evidence() -> None:
    """NEGATIVE CONTROL: the same 68 rows asked about under a different
    oracle count for nothing, so the withholding stands. A pair's record on
    one tree says nothing about the tree a new oracle scores."""
    rows = _rows()
    unpaid = {t: unpaid_pair_buys(rows, t, "0" * len(_ORACLE))
              for t in _LEFTOVERS}
    assert unpaid == dict.fromkeys(_LEFTOVERS, 0)
    assert withholding_paired_tests(
        _LEFTOVERS, unpaid_buys=unpaid) == frozenset(_LEFTOVERS)


def test_a_cashing_row_restarts_the_streak() -> None:
    """A pair that PAID is the paying fill again, and the rows before that
    payment are not evidence against it. Built by marking the recorded row
    at the bound as cashed — the one shape the live record has never held,
    which is why the streak reached 68."""
    rows = _rows()
    idx = len(rows) - DEFAULT_UNPAID_GRACE
    patched = list(rows)
    patched[idx] = RouteLedgerRow(
        **{**{f: getattr(rows[idx], f) for f in (
            "kind", "label", "targets", "bodies_sha", "oracle_fp", "run_id",
            "iteration", "cluster", "started", "minutes", "model_calls",
            "newly_red", "newly_crashed", "kept", "reverted", "target_tests",
            "classes", "share_minutes", "reason")},
           "outcome": RunOutcome.CASHED})
    unpaid = {t: unpaid_pair_buys(patched, t, _ORACLE) for t in _LEFTOVERS}
    assert set(unpaid.values()) == {DEFAULT_UNPAID_GRACE - 1}
    assert withholding_paired_tests(
        _LEFTOVERS, unpaid_buys=unpaid) == frozenset(_LEFTOVERS)
