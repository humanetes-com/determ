"""The schedule-yield reading, pinned on the recorded shape of run
`82925-203ed7b8` (2026-09-28): the per-fn run for one stub deferred in fills
4-6 and cashed in fill 7, two error.rs re-buys spent 61 minutes in fill 7."""
from __future__ import annotations

from datetime import datetime

from pipeline_core.domain.fill_schedule import RouteKind, RouteLedgerRow, RunOutcome
from pipeline_core.domain.schedule_yield import dry_verdict, schedule_yield
from pipeline_core.monitor.schedule_health import (
    ScheduleHealth,
    schedule_health_line,
)

T = (("src/a.rs", "f"),)


def row(it, outcome, minutes=0.0, kind=RouteKind.PER_FN, reason="served", targets=T,
        started="", kept=()):
    return RouteLedgerRow(kind=kind, label=f"{kind} x", targets=targets, bodies_sha="",
                          oracle_fp="", outcome=outcome, run_id="r", iteration=it,
                          minutes=minutes, reason=reason, started=started,
                          net_pass_delta=7 if outcome == RunOutcome.CASHED else 0,
                          kept=kept)


def at(hm: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-29T{hm}:00+00:00")


def recorded():
    rows = []
    for it in (4, 5, 6):
        rows.append(row(it, RunOutcome.DEFERRED, reason="coverage_split"))
        rows.append(row(it, RunOutcome.FLAT, 30, kind=RouteKind.FILE,
                        targets=(("src/b.rs", "g"),)))
    rows.append(row(7, RunOutcome.REVERTED, 61, kind=RouteKind.FILE,
                    reason="rebuy_readmitted", targets=(("src/b.rs", "g"),)))
    rows.append(row(7, RunOutcome.CASHED, 49))
    return rows


def test_the_delayed_payer_and_rebuy_minutes_are_read_off_the_ledger() -> None:
    y = schedule_yield(recorded())
    assert [(p.fill, p.delay_fills, p.reasons) for p in y.payers] == [
        (7, 3, ("coverage_split",))]
    assert y.rebuy_minutes == 61
    assert y.fills[-1].yield_share == 49 / 110


def test_a_delayed_payer_shares_the_deferred_target_not_the_kept_set() -> None:
    """Plan sentence: a cashed run is a delayed payer of an earlier deferral
    of the same kind when their targets overlap. The kept set is what the
    run wrote, which can be a different unit. Negative control: intersecting
    the kept set with the deferral's targets misses it."""
    deferred = row(1, RunOutcome.DEFERRED, reason="coverage_split",
                   targets=(("src/a.rs", "f"),))
    cashed = row(2, RunOutcome.CASHED, 10, targets=(("src/a.rs", "f"),),
                 kept=(("src/b.rs", "g"),))
    y = schedule_yield([deferred, cashed])
    assert [(p.fill, p.delay_fills) for p in y.payers] == [(2, 1)]
    assert not ({("src/b.rs", "g")} & {("src/a.rs", "f")})


def test_dry_minutes_run_from_the_last_cash_even_inside_a_fill_that_cashed() -> None:
    """Run 24951-a72c4642: fill 1 cashed at 05:12 + 18 min, then flat
    refills; at 06:20 the fill had not closed and was 50 min dry."""
    rows = [row(1, RunOutcome.CASHED, 18, started="2026-09-29T05:12:00+00:00"),
            row(1, RunOutcome.FLAT, 13, started="2026-09-29T05:30:00+00:00")]
    y = schedule_yield(rows)
    assert y.fill_net == 7
    d = dry_verdict(y, at("06:20"), minutes=45)
    assert d.burning and round(d.dry_minutes) == 50
    # negative control: the fill-level streak this replaced read 0 here
    assert not any(f.cashed == 0 for f in y.fills)


def test_negative_control_a_recent_cash_is_never_a_burn() -> None:
    rows = [row(1, RunOutcome.CASHED, 30, started="2026-09-29T05:00:00+00:00")]
    assert not dry_verdict(schedule_yield(rows), at("06:00")).burning


def test_nothing_cashed_counts_from_the_run_start() -> None:
    rows = [row(1, RunOutcome.FLAT, 50, started="2026-09-29T05:00:00+00:00")]
    y = schedule_yield(rows)
    assert y.last_cash_end is None
    assert dry_verdict(y, at("06:40"), run_start=at("04:50")).burning
    assert not dry_verdict(y, at("06:00"), run_start=at("04:50")).burning


def test_the_line_flags_burn_and_names_the_run() -> None:
    rows = [row(1, RunOutcome.FLAT, 50, started="2026-09-29T05:00:00+00:00")]
    y = schedule_yield(rows)
    line = schedule_health_line(ScheduleHealth("123-abc", "stable", y,
                                               dry_verdict(y, at("07:00")), ""))
    assert line and line.startswith("123  fill 1 +0  yield 0%") and line.endswith("BURN")
    assert schedule_health_line(None) is None
