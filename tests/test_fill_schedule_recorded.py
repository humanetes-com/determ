"""The fill schedule on the recorded fills — the plan's fixtures, computed.

`tests/fixtures/fills/334902-2774708c_iter11.json` is the 05:23 UTC fill of
2026-09-06 (pass 74, hollow 24, three sessions packed, the process killed
twenty minutes into the first session). `285948-b038af72_iter1.json` is the
first packed fill on the 91-plateau tree (2026-09-05, hollow 0, five
sessions). Both distilled by `tools/distill_fill.py` from `logs/events.jsonl`.

Sentences pinned here, with the fixed pre-schedule walk as the control:
- on the 05:23 fill, the stub-holding runs (file src/error.rs, per-fn)
  open BEFORE the 18-test session, and the session still opens in the same
  fill; under the fixed walk the session opens first;
- on the 91-plateau fill, nothing is demoted: the sessions open first,
  exactly as the fixed walk would;
- the fixture's stub assumption reproduces the plan event's hollow count
  (the check that the fixture is the record, not a hand-made list).
"""
from __future__ import annotations

from tests.fills.harness import candidate_runs, hollow_of, load_fill, replay


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
