"""Pins for tools/flow_audit.py — the flow auditor turns the session-63
manual archaeology (doc channel broken, plateau, wipes) into mechanical
verdicts over events.jsonl + the prompts store."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import flow_audit  # noqa: E402


def _write_run(tmp: Path, events: list[dict], prompts: dict[str, str] | None = None,
               log: str = "") -> tuple[Path, Path, Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    ev = tmp / "events.jsonl"
    ev.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    pdir = tmp / "prompts"
    pdir.mkdir(exist_ok=True)
    for sha, text in (prompts or {}).items():
        (pdir / f"{sha}.txt").write_text(text)
    lg = tmp / "pipeline.log"
    lg.write_text(log)
    return ev, pdir, lg


def _req(ts: str, text: str, prompts: dict[str, str]) -> dict:
    sha = hashlib.sha256(text.encode()).hexdigest()
    prompts[sha] = text
    return {"ts": ts, "event": "llm.request", "data": {"prompt": {"sha256": sha}}}


def _t4(ts: str, iteration: int, count: int) -> dict:
    return {"ts": ts, "event": "task.done", "task": "task4",
            "data": {"ok": True, "current_pass_count": count, "iteration": iteration}}


def test_channel_and_plateau_verdicts(tmp_path: Path) -> None:
    prompts: dict[str, str] = {}
    events = [
        _t4("2026-06-11T01:00:00", 1, 50),
        _req("2026-06-11T01:10:00",
             "Implement ONE Rust function\n<REPOSITORY_SPECIFICATIONS>\nFR-1\n"
             "</REPOSITORY_SPECIFICATIONS>\n<BODY>", prompts),
        _t4("2026-06-11T02:00:00", 2, 50),
        _t4("2026-06-11T03:00:00", 3, 50),
    ]
    ev, pdir, lg = _write_run(tmp_path, events, prompts,
                              log="x\n  Keeping 'anyhow' tree for repair iteration 2\n")
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C1 repo-spec-channel"].status == "PASS"
    assert vs["C4 plateau"].status == "WARN"          # 3× pass=50
    assert vs["C5 no-wipe"].status == "PASS"
    assert vs["C3 floor-monotonicity"].status == "PASS"

    # Broken channel: same events, prompts lack the section.
    prompts2: dict[str, str] = {}
    events2 = [_t4("2026-06-11T01:00:00", 1, 50),
               _req("2026-06-11T01:10:00", "Implement ONE Rust function\n<BODY>", prompts2)]
    ev2, pdir2, lg2 = _write_run(tmp_path / "b", events2, prompts2)
    vs2 = {v.check: v for v in flow_audit.audit(ev2, pdir2, lg2, None, "", 3)}
    assert vs2["C1 repo-spec-channel"].status == "FAIL"


def test_floor_wipe_and_ratchet_verdicts(tmp_path: Path) -> None:
    # Regression WITHOUT a ratchet.restore → C3 FAIL.
    events = [_t4("2026-06-11T01:00:00", 1, 60), _t4("2026-06-11T02:00:00", 2, 40)]
    ev, pdir, lg = _write_run(tmp_path, events,
                              log="  Cleaning up 'anyhow' generated files for retry...\n")
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C3 floor-monotonicity"].status == "FAIL"
    assert vs["C5 no-wipe"].status == "FAIL"

    # Same regression WITH a restore → C3 PASS; ratchet events sane.
    events += [
        {"ts": "2026-06-11T02:00:01", "event": "ratchet.restore",
         "data": {"best": 60, "current": 40, "files": 3}},
        {"ts": "2026-06-11T03:00:00", "event": "ratchet.save",
         "data": {"pass_count": 70, "files": 3}},
    ]
    ev2, pdir2, lg2 = _write_run(tmp_path / "b", events)
    vs2 = {v.check: v for v in flow_audit.audit(ev2, pdir2, lg2, None, "", 3)}
    assert vs2["C3 floor-monotonicity"].status == "PASS"
    assert vs2["C6 ratchet-sanity"].status == "PASS"

    # Invalid ratchet: save that does not improve → C6 FAIL.
    bad = events + [{"ts": "2026-06-11T04:00:00", "event": "ratchet.save",
                     "data": {"pass_count": 70, "files": 3}}]
    ev3, pdir3, lg3 = _write_run(tmp_path / "c", bad)
    vs3 = {v.check: v for v in flow_audit.audit(ev3, pdir3, lg3, None, "", 3)}
    assert vs3["C6 ratchet-sanity"].status == "FAIL"


def test_run_start_parsing_scopes_events(tmp_path: Path) -> None:
    log = ("  PIPELINE STARTED — 2026-06-10 10:00:00 UTC  pipeline.py=aaaa git=bbbb\n"
           "  PIPELINE STARTED — 2026-06-11 06:09:25 UTC  pipeline.py=cccc git=dddd\n")
    assert flow_audit.parse_run_start(log.splitlines()) == "2026-06-11T06:09:25"
    events = [_t4("2026-06-10T12:00:00", 1, 10),   # previous run — must be dropped
              _t4("2026-06-11T07:00:00", 1, 20)]
    ev, pdir, lg = _write_run(tmp_path, events, log=log)
    run = flow_audit.load_run(ev, pdir, lg, "")
    assert flow_audit.iteration_series(run) == [("2026-06-11T07:00:00", 1, 20)]


def test_monitor_renders_flow_audit_row(tmp_path: Path) -> None:
    """Monitor reads the latest flow_audit.report and renders compact verdicts."""
    import monitor

    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({
        "ts": "2026-06-11T10:00:00+00:00", "event": "flow_audit.report",
        "data": {"context": "iter 2", "failed": 1, "verdicts": [
            {"check": "C1 repo-spec-channel", "status": "PASS", "evidence": "ok"},
            {"check": "C5 no-wipe", "status": "FAIL", "evidence": "2 retry wipes"},
        ]},
    }) + "\n")
    old_log, old_start = monitor.EVENTS_LOG, monitor._pipeline_start_time
    monitor.EVENTS_LOG = str(ev)
    monitor._pipeline_start_time = lambda: 0.0
    try:
        data = monitor._latest_flow_audit()
        assert data and data["failed"] == 1
        rows = monitor._flow_audit_rows(data)
        plain = "\n".join(rows)
        assert "C1" in plain and "C5" in plain and "2 retry wipes" in plain
    finally:
        monitor.EVENTS_LOG = old_log
        monitor._pipeline_start_time = old_start


def test_cluster_coverage_verdict(tmp_path: Path) -> None:
    """C7: a task0c cluster pass ending at 0% coverage (skipped augmentation —
    e.g. broken harness) must FAIL; healthy passes PASS; no passes SKIP."""
    ok = {"ts": "2026-06-11T01:00:00", "event": "task.done", "task": "task0c",
          "cluster": "stable", "module": "anyhow",
          "data": {"coverage_pct": 98.1, "covered_lines": 791, "at_ceiling": False}}
    zero = {"ts": "2026-06-11T02:00:00", "event": "task.done", "task": "task0c",
            "cluster": "nightly", "module": "anyhow",
            "data": {"coverage_pct": 0.0, "covered_lines": 0, "at_ceiling": False,
                     "blocked_reason": "harness_broken_after_strip"}}

    ev, pdir, lg = _write_run(tmp_path / "fail", [ok, zero])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C7 cluster-coverage"].status == "FAIL"
    assert "nightly/anyhow" in vs["C7 cluster-coverage"].evidence
    assert "harness_broken_after_strip" in vs["C7 cluster-coverage"].evidence

    ev, pdir, lg = _write_run(tmp_path / "pass", [ok])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C7 cluster-coverage"].status == "PASS"

    ev, pdir, lg = _write_run(tmp_path / "skip", [])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C7 cluster-coverage"].status == "SKIP"
