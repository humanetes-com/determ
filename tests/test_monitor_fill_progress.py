"""What the panel can say about the `fill` step, on a RECORDED fill.

The fixture is `tests/fixtures/monitor/tdd_fill_window.json`: every fill event
of run `1383168-eb151d09` iter 1, cut from `logs/events.jsonl` between the
`phase_c.tdd.iter` at 2026-08-29T21:32:26Z and 23:30Z. That iteration is the
one this module exists for — the sweep took 0.7 s, grade/aim/frontier finished
inside two more, and the fill then ran 1 h 43 m, which is the whole of what the
step strip could report as `▶fill`.

Two of the readings assert the plan's claim against a NEGATIVE CONTROL, and in
both cases the control is the recorded stream itself: it predates the two emits
this change adds, so folding it unchanged reproduces exactly the blindness the
emits close.

Nothing here is a hand-made unit list. The records injected for the post-change
cases are transcriptions of lines that run printed, quoted at each site.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline_core.monitor import render as _render
from pipeline_core.monitor.fill_progress import fold_fill

_FIX = Path(__file__).parent / "fixtures" / "monitor"
_RUN = "1383168-eb151d09"


def _window() -> list[dict[str, Any]]:
    return list(json.loads((_FIX / "tdd_fill_window.json").read_text()))


def _upto(events: list[dict[str, Any]], hhmmss: str) -> list[dict[str, Any]]:
    return [e for e in events if e["ts"][11:19] <= hhmmss]


def _epoch(hhmmss: str) -> float:
    return datetime.fromisoformat(f"2026-08-29T{hhmmss}+00:00").timestamp()


def _plain(s: str) -> str:
    return re.sub("\033\\[[0-9;]*m", "", s)


def _rec(ts: str, event: str, data: dict[str, Any],
         thread: str = "MainThread") -> dict[str, Any]:
    return {"ts": ts, "run_id": _RUN, "event": event, "thread": thread,
            "data": data}


# The two `phase_c.fill.route_plan` records the new emits write for this
# iteration, transcribed from its own log:
#   [phase-c/impl] cluster stable: 1 impl block(s) viable (1 skipped — free
#       fns / oversized). 1 behavioral re-fill block(s) ...
#   [phase-c/file] route order file_primary (model default for ...): 1 file
#       unit(s) dispatch first (2 block(s), free fns and single-block files
#       included).
_ROUTE_PLANS = [
    _rec("2026-08-29T21:32:26.500+00:00", "phase_c.fill.route_plan",
         {"cluster": "stable", "route": "impl", "units": 1, "skipped": 1,
          "refill_units": 1}),
    _rec("2026-08-29T21:32:26.600+00:00", "phase_c.fill.route_plan",
         {"cluster": "stable", "route": "file", "units": 1, "blocks": 2,
          "order": "file_primary"}),
]

# The impl route's five fix-loop attempts, transcribed from the five lines it
# logged between 21:41:11 and 22:29:46 UTC:
#   [phase-c/impl] src/error.rs::Error: compile failed (attempt 1); errs=4
#       compile err(s) codes=['E0277','E0425'] floor=4 compile err(s) stall=0.
#   ... (attempt 2) errs=2 floor=2 stall=0.
#   ... (attempt 3) errs=3 floor=2 stall=1.
#   ... (attempt 4) errs=2 floor=2 stall=2.
#   ... (attempt 5) errs=2 floor=2 stall=3.
#   ... floor stalled at 2 compile err(s) for 3 iter(s) — stopping this block.
_IMPL_ATTEMPTS = [
    _rec(f"2026-08-29T{hhmm}:00.000+00:00", "loop.iter",
         {"name": "phase_c.impl", "cluster": "stable", "file": "src/error.rs",
          "container": "impl Error", "iter": it, "metric_best": floor,
          "improved": stall == 0, "stalled": stall})
    for hhmm, it, floor, stall in (
        ("21:52", 1, "4 compile err(s)", 0),
        ("22:00", 2, "2 compile err(s)", 0),
        ("22:08", 3, "2 compile err(s)", 1),
        ("22:14", 4, "2 compile err(s)", 2),
        ("22:18", 5, "2 compile err(s)", 3),
    )
]


def _merged() -> list[dict[str, Any]]:
    return sorted(_window() + _ROUTE_PLANS + _IMPL_ATTEMPTS, key=lambda e: e["ts"])


# ── the routes and their units, as that iteration actually walked them ───────


def test_the_recorded_fill_is_three_routes_over_the_units_it_dispatched() -> None:
    """The plan's sentence: the fill of iter 1 was the file route over
    `src/error.rs`, then the impl route over `impl Error`, then the per-fn
    route over four fns — and the per-fn route's denominator is the 4 that
    `phase_c.pass.start` published, not the 12 the sweep dispatched.

    The 4 and the 12 differ because a unit that fails one route falls through
    to the next: `context` and `construct_from_boxed` were attempted by all
    three routes that iteration, so summing the routes would count the two
    units the iteration struggled with three times each.
    """
    fill = fold_fill(_window())
    assert [r.id for r in fill.routes] == ["file", "impl", "per-fn"]
    assert fill.batch == 12
    by_id = {r.id: r for r in fill.routes}
    assert by_id["per-fn"].planned == 4
    assert by_id["per-fn"].started == 4
    # 2 of the 4 closed by 23:15 (`construct_from_boxed`, `context` — both
    # `fn.fill.fail`) plus `object_drop` (`fn.fill.done`).
    assert by_id["per-fn"].finished == 3
    assert by_id["file"].started == 1 and by_id["file"].finished == 1
    assert by_id["impl"].started == 1 and by_id["impl"].finished == 1


def test_a_route_that_has_not_run_is_never_marked_done() -> None:
    """Mid-file-first, the impl and per-fn routes have run nothing. The strip
    must not draw a ✓ for either — the marks are observations, so a
    `PHASE_C_ROUTE_ORDER` change cannot make the row lie about which routes
    are finished."""
    fill = fold_fill(_upto(_window(), "21:35:00"))
    assert fill.current == "file"
    assert [r.id for r in fill.routes] == ["file"]
    row = _plain(_render.fill_route_row(fill) or "")
    assert "▶file" in row
    assert "✓" not in row


def test_the_unit_in_flight_is_named_at_every_point_of_the_fill() -> None:
    """The question the row exists to answer — WHICH body is being written —
    at the three points of this fill where the answer differs."""
    for cut, label in (("21:35:00", "src/error.rs"),
                       ("22:20:00", "impl Error"),
                       ("23:05:00", "context")):
        fill = fold_fill(_upto(_window(), cut))
        assert label in {u.label for u in fill.live}, cut


def test_a_new_iteration_clears_the_previous_fill() -> None:
    """`phase_c.tdd.iter` is emitted at the end of the frontier step, so the
    records after the last one are this fill and nothing else. Replaying the
    window with a second iter event appended must leave no route standing."""
    evs = _window() + [_rec("2026-08-29T23:40:00.000+00:00", "phase_c.tdd.iter",
                            {"cluster": "stable", "iter": 2, "dispatched": 12})]
    fill = fold_fill(evs)
    assert fill.routes == () and fill.live == () and fill.current is None
    assert not fill


def test_an_improvement_loop_iter_is_not_folded_into_a_fill_unit() -> None:
    """`loop.iter` is emitted by more than the two fill routes — the
    module-reconstruct improvement loop writes `impl:<file>::<container>` as
    its name, and this run's own events carry 46 of them for
    `impl:src/error.rs::impl Error`. Folding one into the impl route's unit
    would attribute another loop's floor to a body it does not describe."""
    intruder = _rec("2026-08-29T22:05:00.000+00:00", "loop.iter",
                    {"name": "impl:src/error.rs::impl Error", "iter": 9,
                     "metric_best": "99 compile err(s)", "stalled": 9,
                     "improved": False,
                     "file": "src/error.rs", "container": "impl Error"})
    fill = fold_fill(sorted(_upto(_window(), "22:20:00") + [intruder],
                            key=lambda e: e["ts"]))
    unit = next(u for u in fill.live if u.label == "impl Error")
    assert unit.attempt == 0 and unit.floor == "" and unit.stalled == 0


# ── the two emits, each against the recorded stream as its control ───────────


def test_the_impl_blocks_fix_loop_was_dark_and_the_emit_lights_it() -> None:
    """NEGATIVE CONTROL FIRST. `impl Error` held the fill from 21:41:11 to
    22:29:46 UTC — 48 minutes, five attempts and a maximal last-resort call —
    and on the recorded stream the fold can say nothing about any of it: the
    route published `start`, `maximal` and `fail`, and the per-attempt gauge
    only the per-fn route emitted.

    With the same five attempts published (`impl_fill.py::_on_impl_failure`,
    values transcribed from that run's log), the same fold at the same instant
    reads the floor pinned at 2 compile errors with the stall at 3 — the block
    one attempt from being given up on, which is the reading the operator
    could not previously get without opening the log.
    """
    before = fold_fill(_upto(_window(), "22:20:00"))
    dark = next(u for u in before.live if u.label == "impl Error")
    assert (dark.attempt, dark.floor, dark.stalled) == (0, "", 0)

    after = fold_fill(_upto(_merged(), "22:20:00"))
    lit = next(u for u in after.live if u.label == "impl Error")
    assert lit.attempt == 5
    assert lit.floor == "2 compile err(s)"
    assert lit.stalled == 3
    assert lit.improving is False
    row = _plain(_render.fill_unit_rows(after, _epoch("22:20:00"))[0])
    assert "attempt 5" in row and "floor 2 compile err(s)" in row
    assert "stall 3" in row


def test_a_route_with_no_published_plan_draws_no_bar() -> None:
    """NEGATIVE CONTROL FIRST. Without `phase_c.fill.route_plan` the file and
    impl routes have no denominator, so the row says `finished/started seen`
    and draws nothing — it does not borrow the frontier or the dispatch cap.

    With the plan published (`1 impl block(s) viable`, `1 file unit(s)
    dispatch first`, both transcribed from that run's log), the same routes
    read as the fractions of their own plans that they are.
    """
    before = _plain(_render.fill_route_row(fold_fill(_upto(_window(), "22:20:00"))) or "")
    assert "seen" in before
    assert "█" not in before and "░" not in before

    after = _plain(_render.fill_route_row(fold_fill(_upto(_merged(), "22:20:00"))) or "")
    assert "seen" not in after
    assert "✓file 1/1" in after
    assert "▶impl 0/1" in after
    assert "░" in after


def _ev(name: str, ts: str, thread: str = "T1", **data: Any) -> dict[str, Any]:
    return {"event": name, "ts": f"2026-10-11T10:{ts}Z", "thread": thread, "data": data}


def test_session_and_pair_routes_and_attempt_stage() -> None:
    """Sessions and pair fills are routes of the fill, drawn in the order the
    events opened them, beside a unit route that opened first. The unit's
    stage is the newest event its own thread wrote. Event shapes are those
    `slice_session.py` and `impl_fill.py` emit. Negative control: the
    recorded window has neither emit, so it must keep folding to the same
    three-route picture as before. A sort that forced session and pair to
    the front would put them ahead of the file route this stream opened first."""
    site = {"file": "src/a.rs", "fn": "d", "container": ""}
    ev = [
        _ev("phase_c.tdd.iter", "00:00", dispatched=3),
        _ev("phase_c.file.fill.start", "00:00.5", file="src/a.rs"),
        _ev("phase_c.session.start", "00:01", tests=["t_x"], units=[site, site],
            max_turns=4),
        _ev("phase_c.session.turn", "05:00", turn=2, kind="request"),
        _ev("llm.request", "05:01"),
        _ev("phase_c.pair.fill.start", "06:00", thread="T2", dispatcher=site,
            implementer={**site, "fn": "i"}, convicting=["t"]),
        _ev("cargo.spawn", "06:30", thread="T2"),
    ]
    fp = fold_fill(ev)
    ids = [r.id for r in fp.routes]
    assert ids == ["file", "session", "pair"]
    by = {u.route: u for u in fp.live}
    assert (by["session"].attempt, by["session"].max_attempts) == (2, 4)
    assert by["session"].stage == "model" and by["pair"].stage == "cargo"
    ev += [_ev("phase_c.file.fill.done", "06:45", file="src/a.rs"),
           _ev("phase_c.pair.fill.done", "07:00", thread="T2", members_filled=2),
           _ev("phase_c.session.end", "08:00", outcome="kept", turns=2)]
    fp = fold_fill(ev)
    assert not fp.live and all(r.finished == r.started for r in fp.routes)
    assert [r.id for r in fold_fill(_window()).routes
            if r.id in ("session", "pair")] == []


def test_a_terminal_pair_revert_closes_the_row() -> None:
    """A revert that will retry leaves the pair live. A revert that ends
    the run, and a declined pair, close it. Negative control: the retrying
    event on its own does not close the row."""
    site = {"file": "src/a.rs", "fn": "d", "container": ""}
    start = _ev("phase_c.pair.fill.start", "06:00", thread="T2", dispatcher=site,
                implementer={**site, "fn": "i"})
    retrying = _ev("phase_c.pair.fill.reverted", "06:30", thread="T2",
                   why="compile", attempt=1, retrying=True)
    live = fold_fill([_ev("phase_c.tdd.iter", "00:00"), start, retrying])
    assert any(u.route == "pair" for u in live.live)
    terminal = _ev("phase_c.pair.fill.reverted", "07:00", thread="T2",
                   why="u2", attempt=2)
    closed = fold_fill([_ev("phase_c.tdd.iter", "00:00"), start, terminal])
    assert not any(u.route == "pair" for u in closed.live)
    declined = _ev("phase_c.fill.declined", "07:00", thread="T2",
                   route="pair", reason="ignored_inputs")
    gone = fold_fill([_ev("phase_c.tdd.iter", "00:00"), start, declined])
    assert not any(u.route == "pair" for u in gone.live)
