"""Pins for the flow-audit thread relocated from pipeline.py (Plan #77 S4).

FlowAuditor reads the SHARED RunState by reference and reports via injected
log/emit — no pipeline globals. These pin the report gating, the emitted event
shape (failed count over verdicts), and the fail-safe exception swallow.
"""
from __future__ import annotations

from pathlib import Path

import pipeline_core.adapters.flow_audit as fa
from pipeline_core.adapters.flow_audit import FlowAuditor
from pipeline_core.audit import FAIL, PASS, Verdict
from pipeline_core.domain.run_state import RunState


def _auditor(run_state: RunState, log: list[str], emitted: list[tuple[str, dict]]) -> FlowAuditor:
    return FlowAuditor(
        run_state=run_state,
        events_log=lambda: "logs/events.jsonl",
        prompts_dir=Path("logs/prompts"),
        log=log.append,
        emit=lambda ev, **f: emitted.append((ev, f)),
        plateau_n=3)


def test_report_noop_when_audit_disabled(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(fa, "audit", lambda *a, **k: calls.append(1) or [])
    log: list[str] = []
    emitted: list[tuple[str, dict]] = []
    _auditor(RunState(audit_enabled=False), log, emitted).report("ctx")
    assert calls == []      # audit never runs
    assert log == []
    assert emitted == []


def test_report_emits_failed_count(monkeypatch) -> None:
    verdicts = [
        Verdict("C1", PASS, "ok"),
        Verdict("C2", FAIL, "broke"),
        Verdict("C3", FAIL, "broke2"),
    ]
    monkeypatch.setattr(fa, "audit", lambda *a, **k: verdicts)
    log: list[str] = []
    emitted: list[tuple[str, dict]] = []
    _auditor(RunState(audit_enabled=True), log, emitted).report("periodic/900s")
    assert len(emitted) == 1
    event, fields = emitted[0]
    assert event == "flow_audit.report"
    assert fields["data"]["context"] == "periodic/900s"
    assert fields["data"]["failed"] == 2      # two FAIL verdicts
    assert len(fields["data"]["verdicts"]) == 3
    assert any("flow invariants" in line for line in log)


def test_report_is_fail_safe(monkeypatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("events log gone")

    monkeypatch.setattr(fa, "audit", _boom)
    log: list[str] = []
    emitted: list[tuple[str, dict]] = []
    _auditor(RunState(audit_enabled=True), log, emitted).report("ctx")
    assert emitted == []                       # no event on failure
    assert any("skipped" in line for line in log)
