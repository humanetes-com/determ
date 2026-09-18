"""Unit tests for the failure-journal slice: the domain
`FailureEntry` VO + the `FileFailureJournal` adapter that replaced pipeline.py's
`append_failure_log`/`load_failure_log` free functions."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.failure_journal import FileFailureJournal
from pipeline_core.domain.failure_journal import JOURNAL_HEADER, FailureEntry


def test_entry_trend_phrasing() -> None:
    assert FailureEntry(1, "test", 5, 3, "x").trend == "+2 improvement"
    assert FailureEntry(1, "test", 2, 5, "x").trend == "-3 regression"
    assert FailureEntry(1, "test", 4, 4, "x").trend == "no change"


def test_entry_render_format() -> None:
    block = FailureEntry(2, "build", 7, 5, "missing import").render()
    assert "## Iteration 2" in block
    assert "**Failure type:** build" in block
    assert "**Tests passing:** 7 (was 5, +2 improvement)" in block
    assert "**Summary:** missing import" in block


def test_journal_writes_header_once_then_appends(tmp_path: Path) -> None:
    log = tmp_path / "failure_log.md"
    log.touch()
    j = FileFailureJournal()
    j.append_failure_log(log, 1, "test", 3, 0, "first")
    j.append_failure_log(log, 2, "build", 5, 3, "second")
    text = j.load_failure_log(log)
    assert text.count(JOURNAL_HEADER) == 1            # header written exactly once
    assert "## Iteration 1" in text and "## Iteration 2" in text
    assert text.index("Iteration 1") < text.index("Iteration 2")


def test_load_missing_log_is_empty(tmp_path: Path) -> None:
    assert FileFailureJournal().load_failure_log(tmp_path / "absent.md") == ""
