"""The sweep record and the score-climb view the health tab renders from it."""
from __future__ import annotations

from pipeline_core.adapters.run_stats_store import SQLiteRunStatsStore, run_stats_path
from pipeline_core.domain.phase_cost import PhaseKey, PhaseSample, PhaseWork
from pipeline_core.domain.sweep_trajectory import (
    climbs_of,
    fmt_sequence,
    sweep_of,
)
from pipeline_core.monitor.sweep_view import sweep_lines

T0 = "2026-10-15T10:00:00.000+00:00"


def _iter(cluster: str, it: int, passed: int, minute: int, *,
          phase: str = "t2a.fill", stall: int = 0) -> dict[str, object]:
    return {"ts": f"2026-10-15T{10 + minute // 60:02d}:{minute % 60:02d}:00.000+00:00",
            "event": "phase_c.tdd.iter", "in_phase": phase,
            "data": {"cluster": cluster, "iter": it, "pass_count": passed,
                     "fail_count": 146 - passed, "oracle_static": 146,
                     "frontier_size": 40, "stall": stall, "improved": True}}


def test_a_sweep_event_is_read_with_its_clock_and_calls() -> None:
    row = sweep_of(_iter("stable", 3, 58, 90), run_id="r", run_started=T0, llm_calls=41)
    assert row is not None
    assert (row.phase, row.cluster, row.iter, row.pass_count, row.oracle_static) == \
        ("t2a", "stable", 3, 58, 146)
    assert (row.at_ms, row.llm_calls) == (90 * 60_000, 41)


def test_an_iteration_that_measured_nothing_is_no_sweep() -> None:
    early = {"event": "phase_c.tdd.iter", "ts": T0,
             "data": {"cluster": "stable", "iter": 1, "rc": 101, "kind": "no_frontier"}}
    assert sweep_of(early, run_id="r", run_started=T0, llm_calls=0) is None
    assert sweep_of({"event": "llm.request"}, run_id="r", run_started=T0,
                    llm_calls=0) is None


def _rows(run_id: str, readings: list[tuple[int, int, int]], *, cluster: str = "stable",
          phase: str = "t2a.fill") -> list:
    """(pass, minute, calls) per sweep, iter numbered from 1."""
    out = []
    for i, (p, minute, calls) in enumerate(readings, start=1):
        r = sweep_of(_iter(cluster, i, p, minute, phase=phase), run_id=run_id,
                     run_started=T0, llm_calls=calls)
        assert r is not None
        out.append(r)
    return out


def test_a_climb_reports_its_order_high_water_and_cost_from_entry() -> None:
    rows = _rows("r", [(33, 5, 0), (41, 60, 30), (41, 120, 55), (58, 200, 90),
                       (52, 260, 120), (58, 300, 150)])
    (c,) = climbs_of(rows)
    assert c.sequence() == [(33, 1), (41, 2), (58, 1), (52, 1), (58, 1)]
    assert c.best.iter == 4                       # the FIRST sweep at the high-water
    assert c.since_entry(c.best) == (195 * 60_000, 90)
    assert c.regressions == 1
    assert fmt_sequence(c.sequence()) == "33→41×2→58→52→58"


def test_time_between_scores_runs_from_each_score_first_sweep() -> None:
    # 41 is first read at minute 60 and again at 120; 58 arrives at 200, so
    # the 41→58 step took 140 minutes, flat sweeps included.
    rows = _rows("r", [(33, 5, 0), (41, 60, 30), (41, 120, 55), (58, 200, 90),
                       (52, 260, 120), (58, 300, 150)])
    (c,) = climbs_of(rows)
    assert [ms // 60_000 for _, _, ms in c.steps()] == [0, 55, 140, 60, 40]
    assert fmt_sequence(c.steps()) == "33 -55m→ 41×2 -2h20m→ 58 -1h00m→ 52 -40m→ 58"


def test_phases_and_clusters_climb_separately_in_the_order_they_ran() -> None:
    rows = [*_rows("r", [(10, 1, 0), (20, 50, 9)], cluster="nightly"),
            *_rows("r", [(33, 0, 0), (40, 40, 5)]),
            *_rows("r", [(120, 400, 200)], phase="t2c.fill", cluster="merged")]
    assert [(c.phase, c.cluster) for c in climbs_of(rows)] == \
        [("t2a", "stable"), ("t2a", "nightly"), ("t2c", "merged")]


def test_a_long_climb_keeps_its_entry_and_its_latest_readings() -> None:
    seq = [(i, 1) for i in range(30)]
    text = fmt_sequence(seq, limit=6)
    assert text.startswith("0→1→2→") and text.endswith("→27→28→29") and "…24…" in text


def _seed(events, run_id: str, started: str, readings, *, entered: str,
          ended: bool) -> None:
    store = SQLiteRunStatsStore(run_stats_path(events))
    store.open_run(run_id=run_id, crate="anyhow", code_key=f"code-{run_id}",
                   tree_stamp=None, commit="d601f21e3f20", config_fp="cfg", config={},
                   started=started)
    store.add_sample(run_id, PhaseSample(PhaseKey(entered, "anyhow"), "1", None,
                                         started, 1000, "ok", PhaseWork()))
    for r in readings:
        store.add_sweep(r)
    if ended:
        store.close_run(run_id, ended=started, outcome="completed", wall_ms=1,
                        work=PhaseWork())
    store.close()


def test_the_view_lists_recent_runs_newest_first_with_their_climbs(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    _seed(events, "old", "2026-10-14T08:00:00", _rows("old", [(33, 0, 0), (95, 300, 200)]),
          entered="t0c", ended=True)
    _seed(events, "new", T0, _rows("new", [(33, 0, 0), (41, 60, 30), (41, 120, 55)]),
          entered="t2a.blueprint", ended=False)
    lines = sweep_lines(events)
    heads = [i for i, ln in enumerate(lines) if ln.startswith("  new") or ln.startswith("  old")]
    assert [lines[i].split()[0] for i in heads] == ["new", "old"]
    new_head = lines[heads[0]]
    assert "entered t2a.blueprint" in new_head and "unfinished" in new_head
    assert "HEAD=d601f21" in new_head
    climb = lines[heads[0] + 1]
    assert "t2a/stable" in climb and "33 -1h00m→ 41×2" in climb
    assert "best 41/146 at sweep 2 (+1h00m, 30 calls)" in climb
    assert "last 41 stall=0" in climb
    assert "entered t0c" in lines[heads[1]] and "completed" in lines[heads[1]]


def test_a_store_written_before_the_sweeps_table_reads_as_empty(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    store = SQLiteRunStatsStore(run_stats_path(events))
    store._conn.execute("DROP TABLE sweeps")
    store.close()
    lines = sweep_lines(events)
    assert "no sweep recorded yet" in lines[1]
