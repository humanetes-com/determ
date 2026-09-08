"""P25 hung sweep / coverage hang; P26 unbounded wait.

P25 sentence: a coverage `hung inside` line is a row, as is a sweep
`HUNG in`. P26 sentence: a `phase_c.session.turn` then no non-audit event
for 600s is a hit; a hang the sweep scored and restored within the
timeout is not.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p25", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _run(*lines: str, run_id: str = "t") -> object:
    run = census.Run(run_id, "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p25_hits_a_coverage_hang_line() -> None:
    hits = census.detect_p25(_run(
        "  [coverage] 1 binary(ies) did not complete (killed by a signal "
        "or no profile written): lib_gen_1 (rc=-1); hung inside "
        "lib_gen_1::test_hang (killed after 120s); 26 test(s) re-run alone, "
        "26 measured, 0 still unmeasured"), [])
    assert len(hits) == 1
    assert "lib_gen_1::test_hang" in hits[0]["evidence"]


def test_p25_hits_a_sweep_hang_line() -> None:
    hits = census.detect_p25(_run(
        "  [stable] [phase-c/tdd] iter 1: cargo test rc=-1 HUNG in "
        "tests/ptr_gen_1.rs::test_error_debug_chain after 120s"), [])
    assert len(hits) == 1
    assert "test_error_debug_chain" in hits[0]["evidence"]


def _events(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "events.jsonl"
    lines = []
    for ts, run_id, event in rows:
        lines.append(json.dumps({"ts": ts, "run_id": run_id, "event": event}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_p26_hits_a_session_turn_then_silence(tmp_path: Path, monkeypatch) -> None:
    t0 = datetime(2026, 9, 8, 18, 0, 14, tzinfo=UTC)
    t_audit = t0 + timedelta(seconds=900)
    path = _events(tmp_path, [
        (t0.isoformat(), "3500465-67c40e78", "phase_c.session.turn"),
        (t_audit.isoformat(), "3500465-67c40e78", "flow_audit.report"),
    ])
    monkeypatch.setattr(census, "EVENTS_PATH", path)
    monkeypatch.setattr(census, "_p26_now", lambda: (t0 + timedelta(seconds=5400)).timestamp())
    hits = census.detect_p26(_run(run_id="3500465-67c40e78"), [])
    assert len(hits) == 1
    assert "phase_c.session.turn" in hits[0]["evidence"]


def test_p26_is_silent_when_the_next_event_is_within_the_gap(
        tmp_path: Path, monkeypatch) -> None:
    t0 = datetime(2026, 9, 8, 13, 30, 9, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=120)
    path = _events(tmp_path, [
        (t0.isoformat(), "3400546-41aeadee", "phase_c.fill.reading"),
        (t1.isoformat(), "3400546-41aeadee", "phase_c.tdd.iter"),
    ])
    monkeypatch.setattr(census, "EVENTS_PATH", path)
    hits = census.detect_p26(_run(run_id="3400546-41aeadee"), [])
    assert hits == []
