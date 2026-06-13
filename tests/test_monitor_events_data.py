"""P6 data-vs-render split: the monitor's events.jsonl readers (relocated to
pipeline_core.monitor.events_data) roll up the live run correctly and stay
behind the run-start filter."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.monitor import events_data as ed


def _write(p: Path, *recs: dict) -> None:
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _patch(monkeypatch, path: Path, run_start: float = 0.0) -> None:
    monkeypatch.setattr(ed, "EVENTS_LOG", str(path))
    monkeypatch.setattr(ed, "pipeline_start_time", lambda: run_start)


def test_read_recent_events_filters_name_and_run_start(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "x.keep", "data": {"n": 1}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "y.drop", "data": {}},
        {"bad": "json-but-no-event"},
        {"ts": "1999-01-01T00:00:00+00:00", "event": "x.keep", "data": {"old": True}},
    )
    # run_start in 2026 → the 1999 record is dropped, the y.drop is name-filtered.
    _patch(monkeypatch, ev, run_start=1_750_000_000.0)
    got = ed.read_recent_events(lambda n: n == "x.keep")
    assert len(got) == 1 and got[0]["data"]["n"] == 1


def test_coverage_from_events_latest_wins(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "coverage.measured",
         "data": {"pct": 50.0}},
        {"ts": "2026-06-10T00:01:00+00:00", "event": "coverage.measured",
         "data": {"pct": 83.871}},
    )
    _patch(monkeypatch, ev)
    assert ed.coverage_from_events() == "83.9%"


def test_phase_c_state_rollup(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.pass.start",
         "data": {"cluster": "stable", "pass_n": 1, "max_passes": 3, "stubs": 4}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "a", "fix_attempts": 1, "body_chars": 10}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "b", "fix_attempts": 3, "recovered": True, "body_chars": 20}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.fn.fill.fail",
         "data": {"fn": "c", "fix_attempts": 2, "last_error_preview": "error[E0277]: x"}},
        {"ts": "2026-06-10T00:00:04+00:00", "event": "phase_c.note.added", "data": {}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["active"] and s["cluster"] == "stable" and s["pass_n"] == 1
    assert s["fills_done"] == 2 and s["fills_first"] == 1 and s["fills_recovered"] == 1
    assert s["fills_failed"] == 1 and s["notes"] == 1
    assert s["last_error_code"] == "E0277" and s["err_codes"]["E0277"] == 1
    assert s["hot_fn"] == ("b", 3)  # most cumulative attempts


def test_phase_c_state_empty(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(ev, {"ts": "2026-06-10T00:00:00+00:00", "event": "other", "data": {}})
    _patch(monkeypatch, ev)
    assert ed.phase_c_state()["active"] is False


def test_latest_test_and_flow_audit(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "tests.file_stats",
         "data": {"src/a.rs": [1, 0]}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "flow_audit.report",
         "data": {"failed": 0, "verdicts": []}},
    )
    _patch(monkeypatch, ev)
    assert ed.latest_test_file_stats() == {"src/a.rs": [1, 0]}
    assert ed.latest_flow_audit() == {"failed": 0, "verdicts": []}
    assert ed.latest_compile_file_stats() is None  # none emitted


def test_configure_sets_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "EVENTS_LOG", "sentinel")
    target = str(tmp_path / "x.jsonl")
    ed.configure(events_log=target)
    assert target == ed.EVENTS_LOG
