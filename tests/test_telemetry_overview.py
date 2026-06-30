"""C2: emit_per_file_test_overview relocated into the telemetry adapter."""
from __future__ import annotations

from pathlib import Path

import pipeline_core.adapters.telemetry as tel
from pipeline_core.adapters.telemetry import emit_per_file_test_overview


def _capture(monkeypatch):
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tel, "log", lambda m: logs.append(m))
    monkeypatch.setattr(tel, "emit", lambda ev, **kw: events.append((ev, kw)))
    return logs, events


def test_overview_attributes_and_emits(tmp_path: Path, monkeypatch) -> None:
    pd = tmp_path / "proj"
    (pd / "tests").mkdir(parents=True)
    (pd / "tests" / "foo.rs").write_text(
        "#[test] fn t_pass() {}\n#[test] fn t_fail() {}\n", encoding="utf-8")
    logs, events = _capture(monkeypatch)
    output = "test t_pass ... ok\ntest t_fail ... FAILED\n"
    emit_per_file_test_overview(pd, output, context="ctx")
    assert any("[test overview] ctx" in m for m in logs)
    assert events and events[0][0] == "tests.file_stats"
    data = events[0][1]["data"]
    assert data["passed"] == 1 and data["total"] == 2


def test_overview_noop_without_test_lines(tmp_path: Path, monkeypatch) -> None:
    pd = tmp_path / "proj"
    (pd / "tests").mkdir(parents=True)
    logs, events = _capture(monkeypatch)
    emit_per_file_test_overview(pd, "no test result lines here", context="ctx")
    assert events == []
