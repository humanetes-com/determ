"""The fill schedule on the recorded fills — the plan's fixtures, computed.

`tests/fixtures/fills/334902-2774708c_iter11.json` is the 05:23 UTC fill of
2026-09-06 (pass 74, hollow 24, three sessions packed, the process killed
twenty minutes into the first session). `285948-b038af72_iter1.json` is the
first packed fill on the 91-plateau tree (2026-09-05, hollow 0, five
sessions). `1212998-d61fbd2c_iter3.json` is fill 3 of the 2026-09-06 run
that scored 97 (13 never-served runs, packing fitted 0).
`1577437-c93d5c26_iter4.json` is fill 4 of the 2026-09-06 22:15 UTC run,
whose `file src/error.rs` run reverted on attempt 1 (`compile failed;
codes=['E0596']`) and kept on attempt 2 (`FILE FILLED 14 fn(s)`) and was
rowed `REVERTED … kept 0, reverted 11`. Distilled by
`tools/distill_fill.py` from `logs/events.jsonl`.

Sentences pinned here, with the fixed pre-schedule walk as the control:
- on the 05:23 fill, the stub-holding runs (file src/error.rs, per-fn)
  open BEFORE the 18-test session, and the session still opens in the same
  fill; under the fixed walk the session opens first;
- on the 91-plateau fill, nothing is demoted: the sessions open first,
  exactly as the fixed walk would;
- the fixture's stub assumption reproduces the plan event's hollow count
  (the check that the fixture is the record, not a hand-made list);
- on the 22:15 fill, the `file src/error.rs` run whose attempt 1 reverted
  and attempt 2 kept rows FLAT with every dispatched unit kept and none
  reverted, and the next fill ranks it a flat re-buy, not `rebuy_reverted`;
  under the pre-fix bookkeeping (a revert mark no keep clears) the same
  recording rows REVERTED, kept 0, reverted 11.
"""
from __future__ import annotations

from pipeline_core.domain.fill_schedule import (
    TODAY_ORDER,
    RouteKind,
    RunOutcome,
    schedule,
)
from tests.fills.harness import (
    candidate_runs,
    crash_blamed_of,
    hollow_of,
    ledger_of,
    load_fill,
    replay,
    route_attempts_of,
    unmeasurable_of,
)


def test_the_0523_fill_opens_the_stub_runs_before_the_format_session() -> None:
    fix = load_fill("334902-2774708c_iter11")
    assert fix["stubs_source"] == "argument"
    hollow = hollow_of(fix)
    # The fixture is the record: the assumed stubs induce exactly the
    # hollow count the plan event recorded.
    assert len(hollow) == fix["hollow_recorded"] == 24
    runs = candidate_runs(fix)
    sessions = [r.label for r in runs["session"]]
    assert sessions[0].startswith("session[18 test(s)")
    out, opened = replay(fix)
    stub_labels = {r.label for r in runs["file"]} | {r.label for r in runs["per-fn"]}
    first_session = next(i for i, lab in enumerate(opened) if lab in sessions)
    assert all(lab in stub_labels for lab in opened[:first_session])
    assert set(opened[:first_session]) == stub_labels
    assert set(sessions) <= set(opened)          # the sessions still open
    assert out.schedule.ordered[0].level == 0    # blockers first
    # Negative control: the fixed pre-schedule walk opens the session first.
    _out2, opened_fixed = replay(fix, scheduled=False)
    assert opened_fixed[0] == sessions[0]


def test_the_91_plateau_fill_keeps_the_sessions_first() -> None:
    fix = load_fill("285948-b038af72_iter1")
    assert fix["hollow_recorded"] == 0 and not fix["stubs"]
    runs = candidate_runs(fix)
    assert len(runs["session"]) == 5 and not runs["file"] and not runs["per-fn"]
    out, opened = replay(fix)
    _out2, opened_fixed = replay(fix, scheduled=False)
    assert opened == opened_fixed == [r.label for r in runs["session"]]
    assert all(s.level == 1 and s.reason == "never_served" for s in out.schedule.ordered)


def test_a_budget_on_the_0523_fill_defers_nothing_never_served_and_shares_by_weight() -> None:
    fix = load_fill("334902-2774708c_iter11")
    out, opened = replay(fix, budget=120)
    assert not out.schedule.deferred
    shares = [s.share_minutes for s in out.schedule.ordered]
    assert all(sh is not None and sh > 0 for sh in shares)
    assert abs(sum(sh for sh in shares if sh) - 120) < 1e-6
    assert len(opened) == len(out.schedule.ordered)


def _planned(fix: dict) -> list:
    by = candidate_runs(fix)
    return [r for kind in TODAY_ORDER for r in (by.get(kind) or [])]


def test_fill_3_of_the_97_run_ranks_unmeasurable_runs_as_rebuy_flat() -> None:
    """Plan sentence, computed on fill 3 of `1212998-d61fbd2c`.

    Under today's schedule (no unmeasurable args, budget 0): 13 ordered,
    every reason never_served. That is the negative control.

    With unmeasurable = failed names whose binary did not complete, and
    crash_blamed = the locator crash.localized carried: runs disjoint from
    that locator rank rebuy_flat; under a budget they are deferred; runs
    that contain it stay ordered. No pass_count.
    """
    fix = load_fill("1212998-d61fbd2c_iter3")
    runs = _planned(fix)
    rows = ledger_of(fix)
    oracle = rows[0].oracle_fp if rows else "o"
    assert len(runs) == 13
    control = schedule(runs, rows, oracle_fp=oracle, hollow={})
    assert len(control.ordered) == 13 and not control.deferred
    assert {s.reason for s in control.ordered} == {"never_served"}

    unmeas = unmeasurable_of(fix)
    blamed = crash_blamed_of(fix)
    assert blamed and unmeas
    ranked = schedule(
        runs, rows, oracle_fp=oracle, hollow={},
        unmeasurable_tests=unmeas, crash_blamed=blamed)
    staying = [s for s in ranked.ordered if s.run.targets & blamed]
    flat = [s for s in ranked.ordered if not (s.run.targets & blamed)]
    assert staying and all(s.reason == "never_served" for s in staying)
    assert flat and all(s.reason == "rebuy_flat" and s.weight == 0 for s in flat)
    # Computed on this fixture: error.rs file + impl + per-fn contain the
    # blamed locator; the other ten (context/ensure/fmt/kind/ptr file+impl)
    # do not.
    assert len(staying) == 3 and len(flat) == 10

    budgeted = schedule(
        runs, rows, oracle_fp=oracle, hollow={}, budget_minutes=60,
        unmeasurable_tests=unmeas, crash_blamed=blamed)
    assert {s.run.label for s in budgeted.ordered} == {
        s.run.label for s in staying}
    assert {s.run.label for s in budgeted.deferred} == {
        s.run.label for s in flat}

    out, opened = replay(
        fix, budget=60, rows=rows, oracle_fp=oracle,
        unmeasurable_tests=unmeas, crash_blamed=blamed)
    assert {s.run.label for s in out.schedule.deferred} == {
        s.run.label for s in flat}
    assert set(opened) == {s.run.label for s in staying}


def test_fill_4_of_the_2215_run_rows_the_revert_then_keep_as_flat_and_kept() -> None:
    """Plan sentence, computed on fill 4 of `1577437-c93d5c26`: the
    `file src/error.rs` row is FLAT, kept = every unit the run dispatched
    (11 locators — the log's `FILE FILLED 14 fn(s)` counts fn blocks, and
    the four `impl From … for …` blocks share the locator `from`), reverted
    empty; and on the next fill's ledger that row ranks the file
    `rebuy_flat`, never `rebuy_reverted` (weight 0, deferred under any
    budget). Negative control: the pre-fix bookkeeping on the same
    recording rows REVERTED, kept 0, reverted 11, and ranks the next fill's
    file run `rebuy_reverted`.
    """
    fix = load_fill("1577437-c93d5c26_iter4")
    rec = route_attempts_of(fix)["file src/error.rs"]
    # The fixture is the record: the dispatched set is the revert event's
    # planned count, and attempt 1 reverted exactly that set.
    assert len(rec.dispatched) == fix["route_attempts"][0]["planned_recorded"] == 11
    assert [o for o, _u in rec.attempts] == ["reverted", "kept"]
    assert rec.attempts[0][1] == rec.dispatched
    raw = fix["route_attempts"][0]
    assert (raw["recorded_outcome"], raw["recorded_kept"], raw["recorded_reverted"]) == (
        "reverted", 0, 11)

    def error_row(sticky: bool):  # noqa: ANN202 — the row type is the assertion
        log: list[str] = []
        out, _opened = replay(fix, rows=ledger_of(fix), log=log, sticky_reverts=sticky)
        row = next(r for r in out.rows if r.label == "file src/error.rs")
        line = next(ln for ln in log if ln.startswith("  [phase-c/route] file src/error.rs:"))
        return out, row, line

    out, row, line = error_row(False)
    assert row.outcome is RunOutcome.FLAT
    assert frozenset(row.kept) == frozenset((u.file, u.fn_name) for u in rec.dispatched)
    assert row.reverted == () and row.model_calls == 2
    assert "FLAT after" in line and "kept 11, reverted 0" in line
    # The next fill reads this row: the same run under the same identity.
    runs = [r for kind in TODAY_ORDER for r in (candidate_runs(fix).get(kind) or [])]
    nxt = schedule(runs, [*ledger_of(fix), *out.rows], oracle_fp=row.oracle_fp, hollow={})
    ranked = next(s for s in nxt.ordered + nxt.deferred
                  if s.run.kind is RouteKind.FILE and s.run.label == "file src/error.rs")
    assert ranked.reason == "rebuy_flat"

    _out2, old_row, old_line = error_row(True)
    assert old_row.outcome is RunOutcome.REVERTED
    assert old_row.kept == () and len(old_row.reverted) == 11
    assert "REVERTED after" in old_line and "kept 0, reverted 11" in old_line
    nxt_old = schedule(runs, [*ledger_of(fix), *_out2.rows], oracle_fp=row.oracle_fp, hollow={})
    ranked_old = next(s for s in nxt_old.ordered + nxt_old.deferred
                      if s.run.kind is RouteKind.FILE and s.run.label == "file src/error.rs")
    assert ranked_old.reason == "rebuy_reverted"


def test_fill_1_of_the_97_run_s_file_wave_rows_each_run_s_own_landings() -> None:
    """Plan sentence, computed on the 13:55 UTC file wave of fill 1 of
    `1212998-d61fbd2c` (the rows in fill 3's ledger): `file src/context.rs`
    dispatched and reverted its one target, `source`, while `file
    src/error.rs` kept its eight and `file src/ptr.rs` its one; read with
    each run's label, context.rs rows REVERTED, kept 0, reverted 1 — the
    shape its fill-2 row has, where it ran with no sibling keep in its
    window — and the next fill ranks that identity `rebuy_reverted`.
    Negative control: read off the whole window, the context.rs row's
    kept set is exactly the nine foreign locators the ledger recorded, it
    grades FLAT as the ledger did, and the identity ranks `rebuy_flat`."""
    from dataclasses import replace

    from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
    from pipeline_core.application.reconstruct.fill_scheduler import _outcome
    from pipeline_core.domain.fill_attempts import FillAttempts
    from pipeline_core.domain.fill_schedule import RouteRun
    from pipeline_core.domain.models import FnLocator

    fix = load_fill("1212998-d61fbd2c_iter3")
    wave = [r for r in ledger_of(fix)
            if r.iteration == 1 and r.kind is RouteKind.FILE]
    assert [r.label for r in wave] == [
        "file src/context.rs", "file src/error.rs", "file src/ptr.rs"]
    assert len({r.started for r in wave}) == 1          # one wave
    ctx = next(r for r in wave if r.label == "file src/context.rs")
    assert ctx.outcome is RunOutcome.FLAT and len(ctx.kept) == 9
    assert all(f != "src/context.rs" for f, _fn in ctx.kept)
    assert ctx.reverted == (("src/context.rs", "source"),)
    control = next(r for r in ledger_of(fix)
                   if r.iteration == 2 and r.label == "file src/context.rs")
    assert (control.outcome, control.kept, control.reverted) == (
        RunOutcome.REVERTED, (), (("src/context.rs", "source"),))

    def own(row, pairs):  # noqa: ANN001, ANN202 — the row's own file
        file = row.targets[0][0]
        return frozenset(FnLocator(f, fn) for f, fn in pairs if f == file)

    def wave_rows(labelled: bool) -> dict[str, tuple[RouteRunReport, RunOutcome]]:
        ledger = FillAttempts(run_label=lambda: "")
        before = {r.label: ledger.snapshot() for r in wave}   # all open at once
        for r in wave:
            ledger.note_dispatched((FnLocator(f, fn) for f, fn in r.targets), label=r.label)
            ledger.note_call(label=r.label)
            ledger.note_kept(own(r, r.kept), label=r.label)
            ledger.note_reverted(own(r, r.reverted), label=r.label)
        now = ledger.snapshot()
        out = {}
        for r in wave:
            rep = RouteRunReport.from_delta(FillAttempts.since(
                before[r.label], now, r.label if labelled else None), {})
            out[r.label] = (rep, _outcome(rep, 0))
        return out

    def rank(row, outcome, rep) -> str:  # noqa: ANN001 — the next fill's reason
        run = RouteRun(kind=row.kind, label=row.label,
                       targets=frozenset(FnLocator(f, fn) for f, fn in row.targets),
                       target_tests=frozenset(row.target_tests),
                       files=(row.targets[0][0],), bodies_sha=row.bodies_sha)
        new_row = replace(row, outcome=outcome,
                          kept=tuple(sorted((u.file, u.fn_name) for u in rep.kept)),
                          reverted=tuple(sorted((u.file, u.fn_name) for u in rep.reverted)))
        sched = schedule([run], [new_row], oracle_fp=row.oracle_fp, hollow={})
        return (sched.ordered + sched.deferred)[0].reason

    by = wave_rows(True)
    rep, outcome = by["file src/context.rs"]
    assert outcome is RunOutcome.REVERTED and not rep.kept
    assert rep.reverted == {FnLocator("src/context.rs", "source")} and rep.model_calls == 1
    assert rank(ctx, outcome, rep) == "rebuy_reverted"
    rep_e, outcome_e = by["file src/error.rs"]
    assert outcome_e is RunOutcome.FLAT and not rep_e.reverted
    assert rep_e.kept == frozenset(FnLocator(f, fn) for f, fn in ctx.kept if f == "src/error.rs")
    assert len(rep_e.kept) == 8
    rep_p, _o = by["file src/ptr.rs"]
    assert rep_p.kept == {FnLocator("src/ptr.rs", "by_ref")} and not rep_p.reverted

    old = wave_rows(False)
    rep_old, outcome_old = old["file src/context.rs"]
    assert outcome_old is RunOutcome.FLAT
    assert rep_old.kept == frozenset(FnLocator(f, fn) for f, fn in ctx.kept)
    assert rep_old.reverted == {FnLocator("src/context.rs", "source")}
    assert rep_old.model_calls == 3
    assert rank(ctx, outcome_old, rep_old) == "rebuy_flat"
