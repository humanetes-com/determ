"""The panel's two readings of one TDD iteration: which STEP it is in, and
what the declared oracle holds that the sweep's count does not.

Both fixtures are RECORDED — `tests/fixtures/monitor/*.json`, cut from
`logs/events.jsonl` — never a hand-made list shaped to pass. A hand-made pair
vector would be a second copy of the classifier's own rule; the recorded one
is the thing the panel will actually meet.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.monitor import iteration_steps as _steps
from pipeline_core.monitor import render as _render
from pipeline_core.monitor.oracle_gap import oracle_gap

_FIX = Path(__file__).parent / "fixtures" / "monitor"


def _iter_events() -> list[dict[str, Any]]:
    return list(json.loads((_FIX / "tdd_iter_events.json").read_text()))


def _state_of(ev: dict[str, Any]) -> dict[str, Any]:
    """The monitor-state keys `phase_c.tdd.iter` folds into, for one event —
    the same assignments as `events_data.phase_c_state`."""
    d = ev["data"]
    return {
        "tdd_pass": d["pass_count"], "tdd_fail": d["fail_count"],
        "tdd_oracle": d["oracle_size"], "tdd_oracle_static": d["oracle_static"],
        "tdd_incomplete_binaries": d.get("incomplete") or [],
        "tdd_passed_pairs": d.get("passed_pairs") or [],
        "tdd_failed_pairs": d.get("failed_pairs") or [],
    }


# ── the oracle gap: what "unreachable" was hiding ────────────────────────────


def test_the_gap_is_split_by_cause_on_the_recorded_sweeps() -> None:
    """The claim the split exists to make, on the two recorded readings:

      2026-08-28T20:08  static 139, oracle 130 → gap 9,  22 aborted binaries
      2026-08-29T18:27  static 139, oracle 127 → gap 12,  5 aborted binaries

    `never run` is the falsifiable half — it must stay at the SAME 2 across a
    crash count that moves 22 → 5, because it is the two `requires nightly`
    tests (`test_backtrace`, `compiletest`) that this cluster ignores, and
    every other test in the gap is a verdict that was printed and dropped.
    """
    a, b = (_state_of(e) for e in _iter_events())

    ga = oracle_gap(a)
    assert ga is not None and ga.decomposed
    assert (ga.total, ga.crashed_binaries) == (9, 22)
    assert (ga.lost_with_crash, ga.never_ran) == (7, 2)

    gb = oracle_gap(b)
    assert gb is not None and gb.decomposed
    assert (gb.total, gb.crashed_binaries) == (12, 5)
    assert (gb.lost_with_crash, gb.never_ran) == (10, 2)


def test_the_row_no_longer_calls_the_gap_unreachable() -> None:
    """The negative control is the string the row used to render, computed the
    old way on the same recorded event: `12 unreachable (crashed binaries)`.
    It named the wrong tests twice — the crashed binaries' own casualties are
    inside the 44 failing, and 5 of those 12 were tests that PASSED and went
    uncredited — so it must not survive anywhere on the row.
    """
    state = _state_of(_iter_events()[1])
    old = f"{state['tdd_oracle_static'] - state['tdd_oracle']} unreachable"
    assert old == "12 unreachable"          # the control reproduces pre-fix

    score = _render.plain(_render.score_row(state) or "")
    oracle = _render.plain(_render.oracle_row(state) or "")
    assert "unreachable" not in score + oracle
    assert "83/127 passing (65%)" in score
    assert "44 failing" in score
    assert "139 declared" in oracle
    assert "12 outside the count" in oracle
    assert "10 lost with 5 aborted binaries" in oracle
    assert "2 never run" in oracle


def test_a_sweep_with_no_crash_reports_only_the_tests_that_never_ran() -> None:
    """With every binary complete the gap is the cfg-ignored tests alone — the
    reading every stable sweep took from 2026-08-21 to 2026-08-28 (gap 2, zero
    aborted binaries). No aborted-binary clause may appear."""
    state = _state_of(_iter_events()[1])
    state |= {"tdd_oracle": 137, "tdd_incomplete_binaries": [],
              "tdd_pass": 100, "tdd_fail": 37}
    gap = oracle_gap(state)
    assert gap is not None
    assert (gap.total, gap.lost_with_crash, gap.never_ran) == (2, 0, 2)
    assert gap.rendered == "2 outside the count (never run)"
    assert "aborted" not in _render.plain(_render.oracle_row(state) or "")


def test_a_truncated_pair_vector_declines_to_split() -> None:
    """`phase_c.tdd.iter` truncates its pair lists at 200. At the cap the
    split would silently under-count the crashed verdicts, so the row reports
    the gap whole instead of a number that is quietly too small."""
    state = _state_of(_iter_events()[1])
    state["tdd_passed_pairs"] = [["tests/x.rs", f"t{i}"] for i in range(200)]
    gap = oracle_gap(state)
    assert gap is not None and not gap.decomposed
    assert gap.rendered == "12 outside the count"
    assert "lost with" not in _render.plain(_render.oracle_row(state) or "")


def test_no_gap_no_segment() -> None:
    """An intact oracle adds nothing to the row — the nightly cluster's own
    reading (static == oracle) must not render a `0 outside the count`."""
    state = _state_of(_iter_events()[1])
    state |= {"tdd_oracle_static": 127}
    assert oracle_gap(state) is None
    assert _render.oracle_row(state) is None
    assert "outside the count" not in _render.plain(_render.score_row(state) or "")


# ── the step strip ───────────────────────────────────────────────────────────


def _fold(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(event, step) after each recorded event, folded exactly as
    `events_data.phase_c_state` does: an unmapped event leaves the step."""
    step = ""
    out: list[tuple[str, str]] = []
    for e in events:
        got = _steps.step_of_event(e["event"], e.get("data") or {})
        if got is not None:
            step = got
        out.append((e["event"], step))
    return out


def test_the_recorded_boundary_walks_the_iteration_in_order() -> None:
    """One real boundary (cluster stable, 2026-08-29T18:27): the tail of iter
    1's file-first fills, the sweep that follows, and on into the next
    dispatch. The steps must arrive in the loop's own order — and `fill` must
    be reached by the `phase_c.tdd.iter` event, since the fill call is the
    statement after it."""
    doc = json.loads((_FIX / "tdd_iteration_steps.json").read_text())
    walked = _fold(doc["boundary"])

    assert walked[0][1] == "fill"            # mid file-first fill on entry
    # First step after the fill: the sweep publishes no events, so the first
    # thing the stream shows is the reconciliation of the reading it took.
    first_after = next(s for ev, s in walked if s != "fill")
    assert first_after == "grade"
    assert ("phase_c.tdd.oracle_vanished", "grade") in walked
    assert ("phase_c.crash.localized", "aim") in walked
    assert ("phase_c.attribution.escalation_sustained", "aim") in walked
    assert ("phase_c.dispatch.deferred", "frontier") in walked
    assert ("phase_c.re_buy", "frontier") in walked
    assert ("phase_c.tdd.iter", "fill") in walked
    # …and the file-first fills that follow keep it there.
    assert walked[-1] == ("phase_c.verdict", "fill")
    # Order, over the steps the walk actually visited (the crash-determinism
    # re-sweep re-reconciles mid-aim, which is a real backward move and is
    # shown, not smoothed — so this asserts first arrival, not monotonicity).
    firsts = [s for i, (_, s) in enumerate(walked)
              if i == 0 or walked[i - 1][1] != s]
    assert firsts[:5] == ["fill", "grade", "aim", "frontier", "grade"]
    assert firsts[-2:] == ["frontier", "fill"]


def test_an_iteration_that_dispatched_nothing_never_shows_a_fill() -> None:
    """The negative control for the `dispatched` check. `phase_c.tdd.iter` is
    also emitted by the iterations that dispatch nothing and go straight back
    to the sweep — 10 090 of them sit in this event log, the
    `no_frontier_continue` spin. Keyed on the event name alone the strip would
    have claimed a fill phase for every one."""
    doc = json.loads((_FIX / "tdd_iteration_steps.json").read_text())
    ev = doc["no_frontier_continue"]
    assert ev["data"]["kind"] == "no_frontier_continue"
    assert _steps.step_of_event(ev["event"], ev["data"]) == "frontier"
    # …while the dispatching iteration of the same name does advance.
    assert _steps.step_of_event("phase_c.tdd.iter", {"dispatched": 12}) == "fill"


def test_an_unmapped_event_leaves_the_step_alone() -> None:
    """The rule that keeps a new event name from moving the strip by accident.
    Both of these are real names from the same window."""
    assert _steps.step_of_event("phase_c.cluster.prepass_heal_verdict") is None
    assert _steps.step_of_event("phase_c.ledger.reseed") is None
    assert _steps.step_of_event("llm.request") is None


def test_the_sweep_is_read_from_the_log_line_that_starts_it() -> None:
    """The sweep is the one step with no event of its own — cargo is running.
    Its marker is the loop's own line, and only as the log's LAST line."""
    line = "  [stable] [phase-c/tdd] iter 7: running cargo test..."
    assert _steps.step_of_log_line(line) == "sweep"
    assert _steps.step_of_log_line(
        "  [stable] [phase-c/file] src/error.rs: file-first fill") is None
    # The merged-tree phase runs cargo too and is not a TDD iteration step.
    assert _steps.step_of_log_line(
        "  [task2b] running cargo test on merged tree...") is None


def test_the_strip_marks_done_current_and_unreached() -> None:
    """The row reads like the phase strip one level up, and carries the
    time-in-step — the reading that separates a long fill (healthy) from a
    hung sweep (not)."""
    row = _render.iteration_step_row(
        {"tdd_step": "aim", "tdd_step_age_s": 130.0}, now=0.0)
    assert row is not None
    text = _render.plain(row)
    assert "✓sweep  ✓grade  ▶aim  ·frontier  ·fill" in text
    assert "2m in aim" in text
    # No step observed yet → no row; a guessed `sweep` would put a hang on
    # screen that nothing measured.
    assert _render.iteration_step_row({}, now=0.0) is None
    assert _render.iteration_step_row({"tdd_step": "nonsense"}, now=0.0) is None


def test_the_step_age_falls_back_to_the_transition_stamp() -> None:
    """Without a caller-supplied clock the age is measured from the event that
    moved the step, not from the last event inside it."""
    from datetime import datetime
    since = "2026-08-29T18:27:59.531+00:00"
    now = datetime.fromisoformat(since).timestamp() + 45 * 60
    row = _render.iteration_step_row(
        {"tdd_step": "fill", "tdd_step_since": since}, now=now)
    assert row is not None
    assert "45m in fill" in _render.plain(row)
