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
    from pipeline_core.monitor import events_data

    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps({
        "ts": "2026-06-11T10:00:00+00:00", "event": "flow_audit.report",
        "data": {"context": "iter 2", "failed": 1, "verdicts": [
            {"check": "C1 repo-spec-channel", "status": "PASS", "evidence": "ok"},
            {"check": "C5 no-wipe", "status": "FAIL", "evidence": "2 retry wipes"},
        ]},
    }) + "\n")
    old_log, old_start = events_data.EVENTS_LOG, events_data.pipeline_start_time
    events_data.EVENTS_LOG = str(ev)
    events_data.pipeline_start_time = lambda: 0.0
    try:
        data = monitor._latest_flow_audit()
        assert data and data["failed"] == 1
        rows = monitor._flow_audit_rows(data)
        plain = "\n".join(rows)
        assert "C1" in plain and "C5" in plain and "2 retry wipes" in plain
    finally:
        events_data.EVENTS_LOG = old_log
        events_data.pipeline_start_time = old_start


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


def test_harness_fix_effect_verdict(tmp_path: Path) -> None:
    """C8: a claimed harness fix attempt that sent 0 prompts is a no-op loop → FAIL."""
    noop = {"ts": "2026-06-11T01:00:00", "event": "task0c.harness_fix.attempt",
            "data": {"module": "anyhow", "attempt": 1,
                     "broken_files": 3, "files_prompted": 0}}
    acted = {"ts": "2026-06-11T01:01:00", "event": "task0c.harness_fix.attempt",
             "data": {"module": "anyhow", "attempt": 2,
                      "broken_files": 3, "files_prompted": 2}}

    ev, pdir, lg = _write_run(tmp_path / "fail", [noop, acted])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C8 harness-fix-effect"].status == "FAIL"
    assert "0 prompts" in vs["C8 harness-fix-effect"].evidence

    ev, pdir, lg = _write_run(tmp_path / "pass", [acted])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C8 harness-fix-effect"].status == "PASS"

    ev, pdir, lg = _write_run(tmp_path / "skip", [])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C8 harness-fix-effect"].status == "SKIP"


def test_strict_mode_fails_on_skip(tmp_path: Path) -> None:
    """--strict: SKIP = FAIL (full-evidence gate); --allow-skip exempts explicitly."""
    ev, pdir, lg = _write_run(tmp_path, [])   # empty run → everything SKIPs
    base = ["--events", str(ev), "--prompts", str(pdir), "--log", str(lg)]
    assert flow_audit.main(base) == 0                       # lenient: SKIPs pass
    assert flow_audit.main([*base, "--strict"]) == 1        # strict: SKIPs fail
    allow_all = "C1,C2,C3,C4,C5,C6,C7,C8,C9,C10"
    assert flow_audit.main([*base, "--strict", "--allow-skip", allow_all]) == 0


def _t3done(ts: str, passed: bool, **data) -> dict:
    return {"ts": ts, "event": "task.done", "task": "task3",
            "data": {"passed": passed, **data}}


def test_success_evidence_verdicts(tmp_path: Path) -> None:
    """C9 (intent-vs-effect): a 'SUCCESS: all tests passed' claim in the log
    must be backed by the LAST task3 task.done having passed=true. Pins the
    2026-06-12 microcrate run: task3 failed per-cluster (21/24), the fix loop
    validated single-cluster, and the run printed SUCCESS and exited 0."""
    claim_log = ("PIPELINE STARTED — 2026-06-12 04:32:38 UTC\n"
                 "TASK 3 result: fail — 3 failed\n"
                 "SUCCESS: all tests passed after fix loop.\n")

    # The false-SUCCESS shape: claim present, last task3 failed → FAIL.
    ev, pdir, lg = _write_run(
        tmp_path / "false", [_t3done("2026-06-12T05:00:00", False,
                                     pass_count=21, fail_count=3)],
        log=claim_log)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C9 success-evidence"].status == "FAIL"

    # Claim with NO task3 completion at all → FAIL (claim without evidence).
    ev, pdir, lg = _write_run(tmp_path / "noev", [], log=claim_log)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C9 success-evidence"].status == "FAIL"

    # Genuine success: a later task3 passed=true backs the claim → PASS.
    ev, pdir, lg = _write_run(
        tmp_path / "true",
        [_t3done("2026-06-12T05:00:00", False, pass_count=21, fail_count=3),
         _t3done("2026-06-12T05:30:00", True, pass_count=26, fail_count=0)],
        log=claim_log)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C9 success-evidence"].status == "PASS"

    # No claim in the log (run still going / failed honestly) → SKIP.
    ev, pdir, lg = _write_run(
        tmp_path / "none", [_t3done("2026-06-12T05:00:00", False)],
        log="PIPELINE STARTED — 2026-06-12 04:32:38 UTC\nTASK 3 result: fail\n")
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C9 success-evidence"].status == "SKIP"


def _llm(ts: str, event: str, thread: str) -> dict:
    return {"ts": ts, "event": event, "thread": thread}


def test_parallel_stream_integrity_verdicts(tmp_path: Path) -> None:
    """C10: concurrent llm calls on distinctly-named threads PASS; two live
    calls under ONE thread name (the per-thread stream file collision shape —
    e.g. two parallel clusters both spawning 'per-fn-worker-0') FAIL; a fully
    sequential run SKIPs (parallelism never exercised)."""
    # 4-way overlap, distinct thread names → PASS.
    par4 = []
    for i in range(4):
        par4.append(_llm(f"2026-06-12T05:00:0{i}", "llm.request", f"w{i}"))
    for i in range(4):
        par4.append(_llm(f"2026-06-12T05:01:0{i}", "llm.response", f"w{i}"))
    ev, pdir, lg = _write_run(tmp_path / "ok", par4)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "PASS"
    assert "max 4 concurrent" in vs["C10 parallel-streams"].evidence

    # Same-name overlap: second request on w0 before its response → FAIL.
    collide = [
        _llm("2026-06-12T05:00:00", "llm.request", "w0"),
        _llm("2026-06-12T05:00:01", "llm.request", "w0"),
        _llm("2026-06-12T05:00:02", "llm.response", "w0"),
        _llm("2026-06-12T05:00:03", "llm.response", "w0"),
    ]
    ev, pdir, lg = _write_run(tmp_path / "collide", collide)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "FAIL"

    # llm.error closes a call like a response (network failure path).
    errok = [
        _llm("2026-06-12T05:00:00", "llm.request", "w0"),
        _llm("2026-06-12T05:00:01", "llm.request", "w1"),
        _llm("2026-06-12T05:00:02", "llm.error", "w0"),
        _llm("2026-06-12T05:00:03", "llm.request", "w0"),
        _llm("2026-06-12T05:00:04", "llm.response", "w0"),
        _llm("2026-06-12T05:00:05", "llm.response", "w1"),
    ]
    ev, pdir, lg = _write_run(tmp_path / "err", errok)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "PASS"

    # Strictly sequential calls → SKIP (no parallel evidence).
    seq = [
        _llm("2026-06-12T05:00:00", "llm.request", "MainThread"),
        _llm("2026-06-12T05:00:01", "llm.response", "MainThread"),
        _llm("2026-06-12T05:00:02", "llm.request", "MainThread"),
        _llm("2026-06-12T05:00:03", "llm.response", "MainThread"),
    ]
    ev, pdir, lg = _write_run(tmp_path / "seq", seq)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "SKIP"

    # Regression: the anyhow shape (debt #6) — a long-running MainThread call
    # left OPEN while a burst of other calls fires under the SAME 'MainThread'
    # name (concurrent/reentrant emit sharing one per-thread stream file).
    # The long call's response arrives LAST; the burst overlaps it → FAIL, and
    # the offending thread surfaces in the evidence.
    anyhow_shape = [
        _llm("2026-06-13T13:52:09.348", "llm.request", "MainThread"),   # B: long, opens
        _llm("2026-06-13T13:53:18.870", "llm.request", "MainThread"),   # burst overlaps B
        _llm("2026-06-13T13:53:18.936", "llm.request", "MainThread"),
        _llm("2026-06-13T13:53:18.937", "llm.response", "MainThread"),
        _llm("2026-06-13T13:53:18.938", "llm.response", "MainThread"),
        _llm("2026-06-13T13:53:32.344", "llm.response", "MainThread"),  # B closes last
    ]
    ev, pdir, lg = _write_run(tmp_path / "anyhow", anyhow_shape)
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "FAIL"
    assert "MainThread" in vs["C10 parallel-streams"].evidence

    # Events without a thread field (pre-parallelism run) → SKIP, not crash.
    ev, pdir, lg = _write_run(
        tmp_path / "old", [{"ts": "2026-06-12T05:00:00", "event": "llm.request"}])
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "SKIP"

    # Torn events.jsonl line under parallelism → WARN with the count.
    ev, pdir, lg = _write_run(tmp_path / "torn", par4)
    with ev.open("a") as fh:
        fh.write('{"ts": "2026-06-12T05:02:00", "event": "llm.requ\n')
    vs = {v.check: v for v in flow_audit.audit(ev, pdir, lg, None, "", 3)}
    assert vs["C10 parallel-streams"].status == "WARN"
    assert "1 torn" in vs["C10 parallel-streams"].evidence
