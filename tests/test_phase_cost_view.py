"""The health tab's phase-cost section, read from a real store."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pipeline_core.adapters.run_stats_store import SQLiteRunStatsStore, run_stats_path
from pipeline_core.domain.phase_cost import PhaseKey, PhaseSample, PhaseWork
from pipeline_core.monitor import health_view
from pipeline_core.monitor.phase_cost_view import phase_cost_report, sparkline


def _seed(events_log, calls: list[int], *, live_open: bool = False) -> None:
    store = SQLiteRunStatsStore(run_stats_path(events_log))
    for i, c in enumerate(calls):
        rid = f"r{i}"
        store.open_run(run_id=rid, crate="anyhow", code_key=f"code{i}", tree_stamp=None,
                       commit=None, config_fp="cfg", config={}, started=f"2026-10-01T0{i}:00")
        store.set_origin(rid, "clone")
        store.add_sample(rid, PhaseSample(PhaseKey("t0c", "", "stable"), "1", None, "",
                                          600_000, "ok", PhaseWork(llm_calls=c)))
        if i < len(calls) - 1 or not live_open:
            store.close_run(rid, ended="2026-10-01", outcome="completed", wall_ms=1,
                            work=PhaseWork())
    store.close()


def test_the_report_flags_the_regressed_phase_and_draws_its_history(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    _seed(events, [100, 102, 98, 101, 99, 140])
    rep = phase_cost_report(events, running=False)
    assert [f.subject for f in rep.findings] == ["t0c[stable]"]
    row = next(line for line in rep.table if "t0c[stable]" in line)
    assert "regressed" in row and "calls 140 (100)" in row
    assert sparkline((100, 102, 98, 101, 99, 140)) in row
    assert "run r5" in rep.table[0]


def test_no_store_says_so(tmp_path) -> None:
    rep = phase_cost_report(tmp_path / "events.jsonl", running=False)
    assert rep.findings == [] and "no run_stats.db yet" in rep.table[0]


def test_a_live_phase_past_the_p90_is_reported(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    _seed(events, [100, 100, 100, 100, 100, 100], live_open=True)
    started = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    (tmp_path / "phases.jsonl").write_text(json.dumps({
        "event": "phase.enter", "run_id": "r5", "scope": "7", "phase": "t0d",
        "ts": started}) + "\n")
    store = SQLiteRunStatsStore(run_stats_path(events))
    for i in range(5):
        store.add_sample(f"r{i}", PhaseSample(PhaseKey("t0d"), "2", None, "",
                                              600_000, "ok", PhaseWork()))
    store.close()
    rep = phase_cost_report(events, running=True)
    assert any(f.kind == "phase_overrun" and f.subject == "t0d" for f in rep.findings)


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(health_view, "_cache", (0.0, []))
    monkeypatch.setattr(health_view, "_TTL_S", -1.0)


def test_health_lines_put_findings_first_then_the_table(tmp_path, fresh_cache) -> None:
    events = tmp_path / "events.jsonl"
    _seed(events, [100, 102, 98, 101, 99, 140])
    log = tmp_path / "pipeline.log"
    log.write_text("PIPELINE STARTED\n[phase-c/tdd] iter 1: pass=1 fail=1\n")
    lines = health_view.health_lines(log, tmp_path, events)
    first = next(i for i, ln in enumerate(lines) if "phase_cost_regression" in ln)
    table = next(i for i, ln in enumerate(lines) if ln.startswith("Phase cost vs"))
    assert first < table
    assert health_view.health_window(lines, 3, 10**6) == lines[:3]
    assert health_view.health_window(lines, 3, 0) == lines[-3:]
