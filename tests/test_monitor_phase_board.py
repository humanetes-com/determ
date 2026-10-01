"""The phase board and its three views (strip, sub-step row, detailed view).

Fixture: the shape of run `34607-00cca24e` (2026-09-30), an augmented restart
whose `pipeline_state.md` still read `current: task0c/nightly / starting` while
the strip, the bake and the behaviour book ran for 24 minutes. The state file
below is that run's real one; the events are what the announcer now emits over
that stretch. The old strip was driven by the `current:` line alone, so it
could only ever name t0c there; without events the board cannot claim `bake` is
running either (the negative control), and with them it says so.
"""
from __future__ import annotations

from pipeline_core.monitor import phase_board as pb
from pipeline_core.monitor import phase_view as pv
from pipeline_core.monitor.render import plain

STATE_34607 = {
    "cfg_scan": "done", "test_cmd": "done", "task0a-context": "done", "task0a": "done",
    "task0b": "done", "task0d/stable": "done", "task0d/nightly": "done",
    "task0c/stable": "done", "task0c/stable/anyhow": "done",
    "task0c/nightly": "done", "task0c/nightly/anyhow": "done",
}
CLUSTERS = ("stable", "nightly")
T0 = 1_790_000_000.0


def ev(kind, phase, ts, cluster=None, outcome=None, dur=None):
    iso = f"2026-09-30T05:{int(ts) // 60:02d}:{int(ts) % 60:02d}.000+00:00"
    rec = {"event": f"phase.{kind}", "phase": phase, "ts": iso}
    if cluster:
        rec["cluster"] = cluster
    if outcome:
        rec["outcome"] = outcome
    if dur is not None:
        rec["duration_ms"] = dur
    return rec


BAKE_STRETCH = [
    ev("exit", "t0d", 0, "stable", "skipped"), ev("exit", "t0d", 0, "nightly", "skipped"),
    ev("enter", "bake", 1),
    ev("enter", "bake.preserve", 1), ev("exit", "bake.preserve", 2, outcome="ok", dur=1000),
    ev("enter", "bake.api", 2), ev("exit", "bake.api", 3, outcome="ok", dur=1000),
    ev("enter", "bake.extract", 3, "stable"), ev("exit", "bake.extract", 4, "stable", "ok", 1000),
    ev("enter", "bake.surface", 4, "stable"), ev("exit", "bake.surface", 5, "stable", "ok", 1000),
    ev("enter", "bake.harvest", 5, "stable"), ev("exit", "bake.harvest", 6, "stable", "ok", 1000),
    ev("enter", "bake.extract", 6, "nightly"), ev("exit", "bake.extract", 7, "nightly", "ok", 1000),
    ev("enter", "bake.surface", 7, "nightly"), ev("exit", "bake.surface", 8, "nightly", "ok", 1000),
    ev("enter", "bake.harvest", 8, "nightly"), ev("exit", "bake.harvest", 9, "nightly", "ok", 1000),
    # One book for every world, after the per-cluster loop: no cluster.
    ev("enter", "bake.book", 9),
]


def board(records, *, alive=True, state=STATE_34607):
    return pb.fold_board(records, state, alive=alive, clusters=CLUSTERS)


def strip(b, width=140):
    return [plain(r) for r in pv.strip_rows(b, width, now=1_000_000.0)]


def test_state_file_alone_cannot_claim_bake_is_running():
    """Negative control: the data the old strip read. t0c is done, bake is
    pending, nothing is running — the old strip's ▶t0c is unreachable, and so is
    a claim about the bake."""
    b = board([])
    assert pb.running_top(b) is None
    row = strip(b)[0]
    assert "✓t0c" in row and "·bake" in row and "▶" not in row


def test_events_put_the_bake_and_the_book_on_screen_while_they_run():
    b = board(BAKE_STRETCH)
    assert pb.running_top(b).id == "bake"
    top, sub = strip(b)
    assert "▶bake" in top and "✓t0c" in top and "▶t0c" not in top
    assert "✓inline-tests" in sub and "✓api" in sub and "✓skeleton" in sub
    assert "▶book" in sub and "[e] explain" in sub
    assert sub.startswith("  bake")


def test_a_dead_process_reads_aborted_not_running():
    b = board(BAKE_STRETCH, alive=False)
    assert pb.running_top(b) is None
    top, sub = strip(b)
    assert "‖bake" in top and "▶" not in top
    assert "stopped before finishing" in sub


def test_strip_never_wraps_and_keeps_the_current_phase_at_any_width():
    b = board(BAKE_STRETCH)
    for width in (140, 100, 60, 40):
        for row in strip(b, width):
            assert len(row) <= width, (width, row)
        assert "▶bake" in strip(b, width)[0], width


def test_skip_and_state_fallback_are_told_apart():
    b = board([ev("exit", "t0d", 0, "stable", "skipped"),
               ev("exit", "t0d", 0, "nightly", "skipped")])
    top = strip(b)[0]
    assert "↷t0d" in top          # this run skipped it
    assert "✓t0c" in top          # only the state file says so


def test_per_cluster_aggregate_between_clusters_is_running_not_done():
    recs = [ev("enter", "t2a", 0, "stable"), ev("exit", "t2a", 5, "stable", "ok", 5000)]
    b = board(recs)
    st = pb.phase_status(b, next(p for p in pb.TOP_LEVEL if p.id == "t2a"))
    assert st.status == "running" and st.clusters == {"stable": "done", "nightly": "pending"}
    assert board(recs, alive=False).slots  # sanity
    dead = pb.phase_status(board(recs, alive=False),
                           next(p for p in pb.TOP_LEVEL if p.id == "t2a"))
    assert dead.status == "aborted"


def test_a_parent_entering_again_discards_its_stale_steps():
    recs = [ev("enter", "t2a", 0, "stable"), ev("enter", "t2a.fill", 1, "stable"),
            ev("exit", "t2a.fill", 2, "stable", "ok", 1000), ev("exit", "t2a", 3, "stable", "ok", 3000),
            ev("enter", "t2a", 4, "stable")]
    b = board(recs)
    assert ("t2a.fill", "stable") not in b.slots


def test_cluster_row_appears_for_a_per_cluster_phase():
    recs = [ev("enter", "t2a", 0, "stable"), ev("enter", "t2a.fill", 1, "stable")]
    sub = strip(board(recs))[1]
    assert "stable ▶" in sub and "nightly ·" in sub and "▶fill" in sub


def test_detail_view_explains_the_running_phase_and_collapses_finished_ones():
    b = board(BAKE_STRETCH)
    lines = [plain(x) for x in pv.detail_lines(b, 100, now=1_000_000.0)]
    text = "\n".join(lines)
    assert "why: After strip these artefacts are the only source-derived" in text
    assert "steps:" in text and "▶ Behaviour book" in text and "Behaviour book [" not in text
    # finished phases are one line each; the pending ones are explained
    assert sum("Setup" in ln for ln in lines) == 1
    assert "why: Every later phase is scoped per cluster" not in text
    expanded = "\n".join(plain(x) for x in pv.detail_lines(b, 100, 1_000_000.0, expand_all=True))
    assert "why: Every later phase is scoped per cluster" in expanded
    assert len(expanded) > len(text)


def test_detail_view_opens_on_the_running_phase():
    b = board(BAKE_STRETCH)
    lines = [plain(x) for x in pv.detail_lines(b, 100, now=1_000_000.0)]
    start = pv.detail_start(b, 100, now=1_000_000.0)
    assert "bake" in lines[start] and "Bake" in lines[start]


def test_no_events_and_no_state_says_where_the_run_will_start():
    b = pb.fold_board([], {}, alive=False, clusters=())
    assert pb.focus_top(b).id == "setup"
