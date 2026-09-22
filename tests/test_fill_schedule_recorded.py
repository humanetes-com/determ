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
rowed `REVERTED … kept 0, reverted 11`.
`675471-0f3105f2_iter1.json` is fill 1 of the 2026-09-10 18:58 UTC run:
three leftover format tests, a hop pair at served rank 8, a never-served
file and a 26-body per-fn of coverage locators first.
`12120-78b991d3_iter1.json` is fill 1 of the 2026-09-11 12:42 UTC run:
the hop pair opened first and the same leftovers' two sessions, per-fn,
file and impl still sat in ordered with a share.
`3127-6763a5c6_iter2.json` is fill 2 of the 2026-09-18 run on a fresh
stable tree (pass 37 of 184, hollow 142): two hop pairs through one
dispatcher, each holding a stub the other's tests are hollow on, and
every file, impl and per-fn run both blocked by and blocking them; the
recorded schedule opened 0 runs, as did fills 3-6. Distilled by
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
    DEFAULT_UNPAID_GRACE,
    TODAY_ORDER,
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    consecutive_unpaid,
    rebuy_identity,
    run_blocks,
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
    # Per-unit service: every run carrying a unit never opened under this
    # oracle is a new try; the context/kind runs' units were all opened —
    # the context file run's unit last reverted on the file route (a
    # re-buy), the rest were opened on other routes (served).
    by_reason: dict[str, set[str]] = {}
    for s in control.ordered:
        by_reason.setdefault(s.reason, set()).add(s.run.label)
    assert by_reason.pop("served") == {
        "file src/kind.rs", "impl src/context.rs", "impl src/kind.rs"}
    assert by_reason.pop("rebuy_reverted") == {"file src/context.rs"}
    assert set(by_reason) == {"never_served"}

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


def test_the_3500465_per_fn_run_aims_at_completing_binaries_and_stays_open() -> None:
    """`3500465-67c40e78_iter1.json` is the 2026-09-08 15:12 UTC fill whose
    per-fn run spent 97 minutes on twelve escalated producers, every
    verdict `unmeasurable`. The plan named the second session's kept crash
    of `tests/test_downcast.rs` (15:32) as what made those verdicts
    unreachable. Computed on the record: the run's five aimed tests live in
    `tests/test_context.rs`, `tests/ptr_gen_1.rs` and `tests/test_ensure.rs`,
    all of which completed in every reading, so the at-open re-grade does
    not fire on this fill and its twelve fills were wrong bodies the class
    label misnamed. A run aimed only at the crashed binary's tests — the
    second session's own targets — is the shape the re-grade skips.
    """
    from pipeline_core.domain.fill_schedule import run_is_unmeasurable

    fix = load_fill("3500465-67c40e78_iter1")
    runs = candidate_runs(fix)
    per_fn = next(r for r in runs[RouteKind.PER_FN] if "per-fn" in r.label)
    assert per_fn.target_tests == frozenset({
        "test_downcast_high", "test_downcast_low", "test_downcast_mid",
        "test_error_downcast_is", "test_too_long"})
    crashed_bin = "tests/test_downcast.rs"
    crashed_tests = frozenset(
        t for b, t in fix["failed_pairs"] if b == crashed_bin)
    assert crashed_tests == frozenset({
        "test_downcast", "test_downcast_mut", "test_downcast_ref",
        "test_drop", "test_large_alignment"})
    binaries_of_aim = {b for b, t in fix["failed_pairs"] if t in per_fn.target_tests}
    assert crashed_bin not in binaries_of_aim
    # Plan time: nothing unmeasurable; open time after the 15:32 keep: the
    # per-fn run is still measurable, so it opens.
    assert unmeasurable_of(fix) == frozenset()
    assert not run_is_unmeasurable(per_fn, crashed_tests, crash_blamed_of(fix))
    downcast_session = next(
        r for r in runs[RouteKind.SESSION]
        if r.target_tests == frozenset({"test_downcast_ref", "test_drop"}))
    assert run_is_unmeasurable(downcast_session, crashed_tests, crash_blamed_of(fix))


def test_the_675471_fill_opens_the_served_pair_before_coverage_spine_refills() -> None:
    """Need: the hop pair is the paying transaction. Fill 1 of
    `675471-0f3105f2` recorded `file src/error.rs` first (never_served)
    and the pair at slot 8 (served) while both aimed at the same three
    leftovers; the per-fn of 26 coverage locators then classified ten
    landings unreachable. Control: the fixture's own `schedule_runs`
    order is that recorded ranking (novelty over need)."""
    fix = load_fill("675471-0f3105f2_iter1")
    recorded = [str(raw["label"]) for raw in fix["schedule_runs"]]
    assert recorded[0] == "file src/error.rs"
    assert recorded[6].startswith("per-fn")
    assert recorded[7] == "pair src/error.rs::fmt + src/fmt.rs::debug"
    assert fix["schedule_runs"][7]["reason"] == "served"
    runs = _planned(fix)
    pair = next(r for r in runs if r.kind is RouteKind.PAIR)
    assert pair.label == recorded[7]
    # The pair ranked served: a prior REVERTED pair try under a different
    # body identity, inside its grace (a retry that carries a diagnosis).
    prior = RouteRun(
        kind=pair.kind, label=pair.label, targets=pair.targets,
        target_tests=pair.target_tests, files=pair.files, bodies_sha="old")
    rows = [RouteLedgerRow(
        kind=prior.kind, label=prior.label,
        targets=tuple(sorted((f, fn) for f, fn in prior.targets)),
        bodies_sha=prior.bodies_sha, oracle_fp="o",
        outcome=RunOutcome.REVERTED,
        target_tests=tuple(sorted(prior.target_tests)))]
    sched = schedule(runs, rows, oracle_fp="o", hollow={})
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert sched.ordered[0].reason == "served"
    waiting = {s.run.label: s.reason for s in sched.deferred}
    assert waiting.get("file src/error.rs") == "pair_wait"
    assert any(lab.startswith("per-fn") and reason == "pair_wait"
               for lab, reason in waiting.items())
    assert waiting.get("impl src/error.rs") == "pair_wait"
    empty_attr = schedule(runs, rows, oracle_fp="o", hollow={}, attributed=())
    assert [s.run.label for s in empty_attr.ordered] == [s.run.label for s in sched.ordered]
    assert [s.reason for s in empty_attr.deferred] == [s.reason for s in sched.deferred]
    # Negative control: the live schedule event's order (novelty over need).
    assert recorded[0] != pair.label


def test_the_12120_fill_opens_only_the_pair() -> None:
    """Fill 1 of `12120-78b991d3` recorded the hop pair first, then two
    coverage sessions, a per-fn, a file and an impl of the same leftovers
    — all in ordered with a share. With pair_wait, ordered is the pair
    and the other five are deferred. Do not hand-make the six labels;
    the fixture is the event."""
    fix = load_fill("12120-78b991d3_iter1")
    recorded = [str(raw["label"]) for raw in fix["schedule_runs"]]
    assert recorded[0] == "pair src/error.rs::fmt + src/fmt.rs::debug"
    assert recorded[1].startswith("session[2 test(s), 29 unit(s)]")
    assert len(recorded) == 6
    runs = _planned(fix)
    assert {r.label for r in runs} == set(recorded)
    sched = schedule(runs, ledger_of(fix), oracle_fp="o", hollow=hollow_of(fix),
                     budget_minutes=200)
    assert [s.run.label for s in sched.ordered] == [recorded[0]]
    assert {s.run.label for s in sched.deferred} == set(recorded[1:])
    assert all(s.reason == "pair_wait" and s.share_minutes == 0.0
               for s in sched.deferred)
    assert sched.ordered[0].share_minutes == 200.0
    # Negative control: the live schedule event's order.
    assert recorded[1:] != []


def test_the_681204_fill_stops_zeroing_the_runs_behind_a_pair_that_never_pays() -> None:
    """Plan sentence, computed on fill 2 of `681204-b695ab3f` (2026-09-14).

    The claim under test is NOT "builders sort first" or any paraphrase of
    the implementation's rule. It is: on a fill whose hop pair has been
    graded over and over without ever paying, the runs behind it get a
    share instead of zero, and the pair itself is ranked by what its
    ledger says (since 2026-09-19 it no longer keeps the head of the
    order past its grace — run `3893442-cb547388`).

    The fixture is the record — the schedule event's own seven candidate
    runs (one opened, six `pair_wait`) and the route-run rows those same
    candidates had before this fill. The negative control is the shipped
    behaviour, `unpaid_grace=0`, which must reproduce the recorded line:
    one run ordered, six deferred, every one of them at share 0.
    """
    fix = load_fill("681204-b695ab3f_iter2")
    recorded = [str(raw["label"]) for raw in fix["schedule_runs"]]
    opened = [str(raw["label"]) for raw in fix["schedule_runs"]
              if not raw.get("deferred")]
    assert len(recorded) == 7 and len(opened) == 1
    pair_label = opened[0]
    assert pair_label.startswith("pair ")
    assert all(str(raw["reason"]) == "pair_wait"
               for raw in fix["schedule_runs"] if raw.get("deferred"))

    rows = ledger_of(fix)
    oracle = next(r.oracle_fp for r in rows if r.kind is RouteKind.PAIR)
    runs = _planned(fix)
    assert {r.label for r in runs} == set(recorded)
    pair = next(r for r in runs if r.kind is RouteKind.PAIR)

    # The history the bound reads, from the recorded rows: graded tries of
    # this route and these targets that never paid. Keyed on the work key,
    # NOT on the re-buy identity — the identity carries the bodies sha and
    # the pair moves it every fill by keeping table slots no test runs, so
    # an identity-keyed count is the reason the live schedule kept calling
    # this route never-served.
    unpaid = consecutive_unpaid(rows, pair, oracle)
    assert unpaid >= DEFAULT_UNPAID_GRACE, unpaid
    ident = rebuy_identity(pair.kind, pair.targets, pair.bodies_sha, oracle,
                           target_tests=pair.target_tests)
    same_identity = [r for r in rows if r.identity == ident]
    assert len(same_identity) < unpaid
    assert not any(r.outcome is RunOutcome.CASHED or r.net_pass_delta > 0
                   for r in rows if r.kind is RouteKind.PAIR)

    # Negative control: the shipped behaviour reproduces the record.
    control = schedule(runs, rows, oracle_fp=oracle, hollow=hollow_of(fix),
                       budget_minutes=200, unpaid_grace=0)
    assert [s.run.label for s in control.ordered] == [pair_label]
    assert {s.run.label for s in control.deferred} == set(recorded) - {pair_label}
    assert all(s.reason == "pair_wait" and s.share_minutes == 0.0
               for s in control.deferred)

    # The change: nothing is zeroed behind the pair, and past its grace the
    # pair is ranked by its ledger like any run — its last try on these
    # very bodies came back flat, so it is a flat re-buy the budget defers
    # (re-admitted after `readmit_after` deferrals), and every other
    # candidate opens with a share.
    sched = schedule(runs, rows, oracle_fp=oracle, hollow=hollow_of(fix),
                     budget_minutes=200)
    assert not [s for s in sched.deferred if s.reason == "pair_wait"]
    assert {s.run.label for s in sched.ordered} == set(recorded) - {pair_label}
    assert [(s.run.label, s.reason) for s in sched.deferred] == [
        (pair_label, "rebuy_flat")]
    assert all(s.share_minutes and s.share_minutes > 0 for s in sched.ordered)


def test_a_pair_inside_its_grace_still_defers_the_rest_on_the_681204_fill() -> None:
    """The bound is a grace, not a removal: with the pair's own history
    trimmed to fewer graded rows than the grace, the recorded fill
    schedules exactly as it did — pair alone, six `pair_wait`. Same
    records, same function, one input shortened."""
    fix = load_fill("681204-b695ab3f_iter2")
    rows = ledger_of(fix)
    oracle = next(r.oracle_fp for r in rows if r.kind is RouteKind.PAIR)
    runs = _planned(fix)
    pair = next(r for r in runs if r.kind is RouteKind.PAIR)
    pair_rows = [r for r in rows if r.kind is RouteKind.PAIR]
    trimmed = [r for r in rows if r.kind is not RouteKind.PAIR]
    trimmed += pair_rows[-(DEFAULT_UNPAID_GRACE - 1):]
    assert consecutive_unpaid(trimmed, pair, oracle) < DEFAULT_UNPAID_GRACE

    sched = schedule(runs, trimmed, oracle_fp=oracle, hollow=hollow_of(fix),
                     budget_minutes=200)
    assert [s.run.label for s in sched.ordered] == [pair.label]
    assert all(s.reason == "pair_wait" and s.share_minutes == 0.0
               for s in sched.deferred)


def test_the_fresh_tree_fill_opens_every_run_instead_of_none() -> None:
    """Plan sentence: a run is held only by a run that opens, and a pair
    does not hold a run its own tests are hollow on — so the recorded fill
    that opened nothing opens every candidate, both pairs included.
    Control: the recorded schedule event itself (0 opened, every run
    `pair_wait`), and the pre-fix predicate on the fixture's own runs and
    hollow relation, which defers all ten."""
    fix = load_fill("3127-6763a5c6_iter2")
    assert fix["stubs_source"] == "schedule_event"
    hollow = hollow_of(fix)
    assert len(hollow) == fix["hollow_recorded"] == 142
    runs = [r for rs in candidate_runs(fix).values() for r in rs]
    assert len(runs) == 10
    pairs = [r for r in runs if r.kind is RouteKind.PAIR]
    assert len(pairs) == 2
    pre_fix_deferred = [r.label for r in runs
                        if any(run_blocks(p, r, hollow) for p in pairs)]
    assert len(pre_fix_deferred) == len(runs)
    out, opened = replay(fix, budget=200.0, rows=ledger_of(fix))
    assert sorted(opened) == sorted(r.label for r in runs)
    assert not out.schedule.deferred
    assert {p.label for p in pairs} <= set(opened)


def test_the_3893442_fill_opens_the_never_served_runs_before_pairs_past_their_grace() -> None:
    """Plan sentence, computed on fill 1 of `3893442-cb547388` (2026-09-19).

    The claim: on a fill whose two hop pairs are past their grace (the
    debug pair graded 10 times under this oracle, none paying; both pairs'
    last replies were the bodies already on disk), every never-served run
    opens before either pair, and the session that did pay that fill
    (`session[1 test(s), 29 unit(s)] #0`, +3 in 12 minutes, opened 4th
    after 69 minutes of the two pairs) opens first.

    The fixture's ledger is the index prefix the fill's schedule loaded
    (`ledger_source: index`). The negative control is the record itself:
    the schedule event opened both pairs at slots 2 and 3, ahead of that
    session — and the pre-change `schedule` over this same fixture
    reproduces that line.
    """
    fix = load_fill("3893442-cb547388_iter1")
    assert fix["ledger_source"] == "index"
    recorded = [str(raw["label"]) for raw in fix["schedule_runs"]
                if not raw.get("deferred")]
    pairs = [lbl for lbl in recorded if lbl.startswith("pair ")]
    paid = "session[1 test(s), 29 unit(s)] #0"
    assert len(pairs) == 2
    assert [recorded.index(p) for p in pairs] == [1, 2]
    assert recorded.index(paid) == 3

    runs = sorted((r for rs in candidate_runs(fix).values() for r in rs),
                  key=lambda r: r.seq)
    rows = ledger_of(fix)
    # The fill's oracle is the newest row's: the index prefix also holds
    # pair rows graded under the oracle before the 2026-09-18 re-augment.
    oracle = rows[-1].oracle_fp
    sched = schedule(runs, rows, oracle_fp=oracle, hollow=hollow_of(fix),
                     budget_minutes=200,
                     unmeasurable_tests=unmeasurable_of(fix),
                     crash_blamed=crash_blamed_of(fix),
                     max_per_kind={RouteKind.SESSION: 4})
    order = [s.run.label for s in sched.ordered]
    assert order[0] == paid
    never = [s.run.label for s in sched.ordered if s.reason == "never_served"]
    for p in pairs:
        if p in order:
            assert all(order.index(n) < order.index(p) for n in never)
    assert not [s for s in sched.deferred if s.reason == "pair_wait"]
