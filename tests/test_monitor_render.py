"""R7 regression tests for monitor.py's render-half text helpers (ANSI strip,
pipeline_state done-key, CALL_START marker) — each pinned to the regex it replaced."""
from __future__ import annotations

import re

import monitor as m
from pipeline_core.monitor import render as _render


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

    for line in ["[stable] done\n", "[task0c/nightly]   done extra", "[x] donezo",
                 "[a/b/c] done", "[only]done", "no bracket", "[ ] done", "[k]\tdone"]:
        assert m._state_done_key(line) == old(line), repr(line)


def test_call_start_value_matches_old_regex() -> None:
    old_re = re.compile(r'\[CALL_START: ([\d.]+)\]')

    def old(c):
        mm = old_re.search(c)
        return mm.group(1) if mm else None

    for c in ["[CALL_START: 12.34]", "x [CALL_START: 5] y", "[CALL_START: ]",
              "[CALL_START: 1.2.3]", "no marker", "[CALL_START: abc]", "[CALL_START: 9"]:
        assert m._call_start_value(c) == old(c), repr(c)


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
    # Numerator can pass the batch size: bar caps, excess labeled apart.
    state["pass_fills_done"] = 40
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "34/34 unit(s)" in text and "+15 beyond frontier" in text
    assert "49/34" not in text
    # No batch snapshot (per-fn short-circuit emits no pass.start) → no row.
    state["pass_stubs_total"] = 0
    text = _render.plain("\n".join(_render.iteration_progress_rows(state, now)))
    assert "sweep" not in text
