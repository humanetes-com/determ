"""Regression tests for monitor.py's render-half text helpers (ANSI strip,
pipeline_state done-key, CALL_START marker) — each pinned to the regex it replaced."""
from __future__ import annotations

import re

import monitor as m
from pipeline_core.monitor import render as _render
from pipeline_core.monitor.frame import SplitLayout


def test_plain_strips_ansi_like_old_regex() -> None:
    old = lambda s: re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)  # noqa: E731
    for s in ["\033[31mred\033[0m", "plain", "\033[1;32mx\033[0my\033[m",
              "\033[bad", "a\033[99Hb"]:
        assert m._plain(s) == old(s), repr(s)


def test_state_done_key_matches_old_regex() -> None:
    old_re = re.compile(r"\[([^\]/]+)(?:/[^\]]*)?\]\s+done\b")

    def old(line):
        mm = old_re.match(line)
        return mm.group(1) if mm else None

    # Through the render module, not a monitor.py alias: the pipeline_state
    # read moved to run_data.phase_progress with the phase strip, and
    # monitor.py no longer imports this helper.
    for line in ["[stable] done\n", "[task0c/nightly]   done extra", "[x] donezo",
                 "[a/b/c] done", "[only]done", "no bracket", "[ ] done", "[k]\tdone"]:
        assert _render.state_done_key(line) == old(line), repr(line)


def test_call_start_value_matches_old_regex() -> None:
    old_re = re.compile(r'\[CALL_START: ([\d.]+)\]')

    def old(c):
        mm = old_re.search(c)
        return mm.group(1) if mm else None

    # Called through the render module, not a monitor.py alias: the marker is
    # read by the generation panel and the worker rows now, and monitor.py
    # itself no longer imports it.
    for c in ["[CALL_START: 12.34]", "x [CALL_START: 5] y", "[CALL_START: ]",
              "[CALL_START: 1.2.3]", "no marker", "[CALL_START: abc]", "[CALL_START: 9"]:
        assert _render.call_start_value(c) == old(c), repr(c)


def test_driver_counter_prefers_the_live_outer_loop() -> None:
    """`pass=?/?` was rendered for hours on healthy tdd runs: the per-fn engine
    emits no pass.start once the frontier is all re-fills. The tdd iter wins."""
    tdd = {"driver": "tdd", "tdd_iter": 4, "tdd_stall": 2, "tdd_max_stalls": 3,
           "pass_n": None, "max_passes": None}
    assert _render.driver_counter(tdd) == "iter=4  stall=2/3"
    # Budget omitted until an iter event carries it (older runs) — never "?/?".
    assert _render.driver_counter({**tdd, "tdd_max_stalls": None}) == "iter=4"
    # An unreported iter shows "?" — a fabricated "iter=1" would be the same
    # class of lie as the "?/?" this replaced.
    assert _render.driver_counter({"driver": "tdd", "tdd_iter": 0}) == "iter=?"
    per_fn = {"driver": "per-fn", "pass_n": 2, "max_passes": 3}
    assert _render.driver_counter(per_fn) == "pass=2/3"
    assert _render.driver_counter({}) == "pass=?/?"


def test_iteration_progress_rows_move_inside_the_iteration() -> None:
    """The row must measure the inside of the iteration — pace vs the run's
    median iteration length, the fills counter, last-fill age, frontier —
    and must NOT repeat the pass count the tdd row already carries."""
    from datetime import datetime
    t1, t2 = "2026-08-09T10:00:00+00:00", "2026-08-09T10:30:00+00:00"
    now = datetime.fromisoformat(t2).timestamp() + 600
    state = {
        "tdd_pass": 76, "tdd_fail": 28, "tdd_iter": 3, "tdd_frontier": 34,
        "tdd_iter_ts": [t1, t2],
        "last_fill_ts_iso": "2026-08-09T10:38:00+00:00",
        "total_fns_in_cluster": 223, "filled_count_at_pass_start": 180,
        "all_stubs_remaining_at_pass_start": 43,
        "pass_fills_done": 10, "pass_fills_failed": 25, "pass_stubs_total": 22,
    }
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "10m of ~30m typical" in text
    assert "fills 35" in text and "last 2m ago" in text
    assert "frontier 34 fn(s)" in text
    assert "passing" not in text and "76" not in text
    # Initial fill still climbing → bodies bar present, straight from the
    # pass.start snapshot (never snapshot + fill units — see the re-fill
    # inflation test below). 180+43 fillable == the 223 total here, so no
    # cfg-gated suffix.
    assert "180/223 fillable (80%) · 43 hollow" in text
    assert "cfg-gated" not in text


def test_iteration_progress_rows_refill_units_never_hide_the_bodies_bar() -> None:
    """Fill units count behavioral re-fills of already-filled bodies, so
    snapshot + units overflows the total: on run 3368-753757e6 iter 3
    (2026-08-15, 197 filled of 209 fillable, 12 hollow, 14 cfg-gated to the
    nightly cluster) done reached 130, min(223, 197+130) read 223, "hollow"
    read 0, and the bodies bar hid itself with 12 fns genuinely hollow. The
    bar must render the snapshot over the FILLABLE denominator and stay
    visible while any fillable fn is hollow."""
    state = {
        "tdd_iter_ts": ["2026-08-09T10:00:00+00:00"], "tdd_frontier": 81,
        "total_fns_in_cluster": 223, "filled_count_at_pass_start": 197,
        "all_stubs_remaining_at_pass_start": 12,
        "pass_fills_done": 130, "pass_fills_failed": 6, "pass_stubs_total": 21,
    }
    from datetime import datetime
    now = datetime.fromisoformat("2026-08-09T12:00:00+00:00").timestamp()
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "197/209 fillable (94%) · 12 hollow · 14 cfg-gated elsewhere" in text


def test_iteration_progress_rows_saturated_tree_drops_the_bodies_bar() -> None:
    """A `223/223 (100%)` bar sat unchanged for hours while the loop rewrote
    WRONG bodies — once every fn has a body the bodies row must disappear,
    not fake completion."""
    state = {
        "tdd_pass": 76, "tdd_fail": 28, "tdd_iter": 5, "tdd_frontier": 34,
        "tdd_iter_ts": ["2026-08-09T10:00:00+00:00"],
        "total_fns_in_cluster": 223, "filled_count_at_pass_start": 223,
        "all_stubs_remaining_at_pass_start": 0,
        "pass_fills_done": 40, "pass_fills_failed": 25, "pass_stubs_total": 22,
    }
    from datetime import datetime
    now = datetime.fromisoformat("2026-08-09T10:42:00+00:00").timestamp()
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "bodies" not in text and "100%" not in text
    # First iter of the run: elapsed with no median yet — never a fake bar.
    assert "42m elapsed (first iter, no typical yet)" in text
    assert "fills 65" in text


def test_iteration_progress_rows_never_join_fills_and_queue_as_fraction() -> None:
    """`frontier: 65/22` invited reading '65 out of 22': fills count re-fills
    and retries while queue/frontier count fns, so A>B is normal. The
    counters must never be rendered slash-joined."""
    state = {
        "tdd_iter_ts": ["2026-08-09T10:00:00+00:00"], "tdd_frontier": 34,
        "total_fns_in_cluster": 223, "filled_count_at_pass_start": 223,
        "all_stubs_remaining_at_pass_start": 0,
        "pass_fills_done": 40, "pass_fills_failed": 25, "pass_stubs_total": 22,
    }
    from datetime import datetime
    now = datetime.fromisoformat("2026-08-09T10:01:00+00:00").timestamp()
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "fills 65" in text
    assert "65/22" not in text and "65/34" not in text


def test_iteration_progress_rows_sweep_bar_tracks_the_batch() -> None:
    """Once every fn has a body the bodies bar retires, and the panel showed
    no forward motion inside an iteration (9 h of iter 3, run
    1159463-b1e352e5, 2026-08-09). The sweep row is that motion: completed
    fill units against the batch the sweep opened with — the same counter the
    pipeline log prints as `N/34`. Overflow past the batch (file/impl passes
    count members) is capped and labeled, never slash-joined."""
    state = {
        "tdd_iter_ts": ["2026-08-09T10:00:00+00:00"], "tdd_frontier": 34,
        "total_fns_in_cluster": 223, "filled_count_at_pass_start": 223,
        "all_stubs_remaining_at_pass_start": 0,
        "pass_fills_done": 22, "pass_fills_failed": 9, "pass_stubs_total": 34,
    }
    from datetime import datetime
    now = datetime.fromisoformat("2026-08-09T10:01:00+00:00").timestamp()
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "31/34 unit(s)" in text and "22 ✓" in text and "9 ✗" in text
    # Past the batch there is NO bar. Capping it and labelling the excess (what
    # this assertion used to require) leaves a row pinned at 100% for the rest
    # of the iteration: run 669347-cf33eae3 (2026-08-15) held `69/69 frontier
    # fn(s) … +10 beyond frontier` for 46 minutes with four workers busy, which
    # is indistinguishable from a hung loop. Throughput replaces it.
    state["pass_fills_done"] = 40
    sweep = next(ln for ln in _render.plain(
        "\n".join(_render.iteration_progress_rows(state, now))).splitlines()
        if "sweep" in ln)
    assert "49 unit(s)" in sweep and "40 ✓" in sweep and "9 ✗" in sweep
    assert "49.0/min" in sweep                      # 49 units in the 1 min elapsed
    assert "past this iter's 34 unit(s)" in sweep   # the batch is named, not divided into
    assert "34/34" not in sweep and "49/34" not in sweep
    assert "█" not in sweep and "░" not in sweep    # no bar at all past the batch
    # Neither batch known (no pass.start, no frontier) → still no BAR: there is
    # nothing to divide by, and a bar over a guessed denominator is worse than
    # no bar. The counts and the rate do not need a denominator and stay — a
    # row that vanishes is the "the monitor shows nothing" complaint, and
    # "49 units, 40 ✓, 49/min" claims no total.
    state["pass_stubs_total"] = 0
    state["tdd_frontier"] = None
    sweep = next(ln for ln in _render.plain(
        "\n".join(_render.iteration_progress_rows(state, now))).splitlines()
        if "sweep" in ln)
    assert "49 unit(s)" in sweep and "49.0/min" in sweep
    assert "█" not in sweep and "░" not in sweep and "past this iter" not in sweep


def test_iteration_progress_rows_sweep_bar_falls_back_to_the_frontier() -> None:
    """A fully-bodied tree emits no pass.start, and the sweep bar was keyed
    to it alone — so the one row that is supposed to keep moving after the
    `bodies` bar retires vanished exactly there.

    Run 500301-6a66bf86 (2026-08-15) iter 1: 61-fn frontier, all behavioural
    re-fills, dispatched through the file-first route as nine file units each
    logging `0 stub(s), N re-fill(s)`. The per-fn engine never ran, so
    `pass_stubs_total` was 0 with 35 fills landed and 16 minutes elapsed, and
    the panel rendered ONE row and no bar of any kind. The frontier is the
    batch that sweep opened with; the row must use it, and must say so — the
    two denominators count different populations.
    """
    state = {
        "tdd_iter_ts": ["2026-08-15T08:48:51+00:00"], "tdd_frontier": 61,
        "total_fns_in_cluster": 0, "filled_count_at_pass_start": 0,
        "all_stubs_remaining_at_pass_start": 0,
        "pass_fills_done": 35, "pass_fills_failed": 0, "pass_stubs_total": 0,
    }
    from datetime import datetime
    now = datetime.fromisoformat("2026-08-15T09:04:51+00:00").timestamp()
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "sweep" in text
    assert "35/61 frontier fn(s)" in text and "35 ✓" in text
    # Named apart from the stub-snapshot batch, which counts fill units.
    assert "35/61 unit(s)" not in text
    # The snapshot is genuinely absent here (0/0) — no bodies bar may be
    # fabricated from it, and none may be inferred either.
    assert "bodies" not in text
    # A real snapshot still wins the denominator: same state, pass.start seen.
    # 35 units against a 21-unit batch is past it, so the row is the throughput
    # form and names the batch it passed — with the stub-snapshot's unit(s),
    # never the frontier's fns.
    state["pass_stubs_total"] = 21
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "35 unit(s)" in text and "past this iter's 21 unit(s)" in text
    assert "frontier fn(s)" not in text and "21/21" not in text


def _render_frame(monkeypatch, cols: int, split: bool, view: str) -> list[str]:
    monkeypatch.setattr(m, "_term_w", lambda: cols)
    monkeypatch.setattr(m, "_h", lambda: 30)
    monkeypatch.setattr(m, "_layout", SplitLayout(enabled=split))
    monkeypatch.setattr(m, "_view", view)
    monkeypatch.setattr(m, "gpu", lambda: ["gpu row " + "x" * 300])
    monkeypatch.setattr(m, "pipeline_status", lambda width: ["pipeline row " + "y" * 300])
    monkeypatch.setattr(m, "generation", lambda width: ["gen row"])
    m._buf.clear()
    m._render_all()
    frame = list(m._buf)
    m._buf.clear()
    return frame


def test_split_keeps_monitor_left_and_detail_right(monkeypatch) -> None:
    # A wide terminal with split on: every body row is exactly the terminal
    # width (both columns padded or cut to their own width), the left column
    # carries the monitor boxes and the right the selected detail view.
    frame = _render_frame(monkeypatch, 200, True, "legend")
    left_w, right_w = m._split_widths() or (0, 0)
    assert left_w + 1 + right_w == 200
    assert left_w <= 200 * 30 // 100 < right_w   # the overview is the narrow column
    body = [m._plain(r) for r in frame[:-m.BOTTOM_LINES]]
    assert len(body) == 30 - m.BOTTOM_LINES
    assert {len(r) for r in body} == {200}
    assert body[0][:left_w].startswith("┌─ GPU")
    assert body[0][left_w + 1:].startswith("┌─ Legend")
    assert body[0][left_w - 1] == "┐" and body[0][-1] == "┐"


def test_split_falls_back_to_tabs_when_too_narrow(monkeypatch) -> None:
    frame = _render_frame(monkeypatch, 100, True, "legend")
    assert m._split_widths() is None
    plain = [m._plain(r) for r in frame]
    assert plain[0].startswith("┌─ GPU")
    assert any(r.startswith("┌─ Legend") for r in plain)
    assert not any("┐ ┌" in r for r in plain)   # no second column
