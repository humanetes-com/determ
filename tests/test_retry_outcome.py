"""SourceSnapshot capture/restore + retry verdict logic (P3.D), pure."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.retry_outcome import (
    RetryMode,
    RetryVerdict,
    build_verdict,
    failing_test_verdict,
)
from pipeline_core.domain.source_snapshot import SourceSnapshot


def test_snapshot_restores_edits(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "lib.rs"
    f.write_text("ORIGINAL\n")
    snap = SourceSnapshot.capture(src)
    assert snap
    f.write_text("CORRUPTED\n")
    snap.restore()
    assert f.read_text() == "ORIGINAL\n"


def test_snapshot_absent_root_is_falsy(tmp_path: Path) -> None:
    assert not SourceSnapshot.capture(tmp_path / "nope")


def test_build_verdict() -> None:
    assert build_verdict(initial_errors=3, final_errors=5, has_snapshot=True) is RetryVerdict.REVERT
    assert build_verdict(initial_errors=3, final_errors=5, has_snapshot=False) is RetryVerdict.ESCALATE
    assert build_verdict(initial_errors=3, final_errors=1, has_snapshot=True) is RetryVerdict.RECURSE
    assert build_verdict(initial_errors=3, final_errors=3, has_snapshot=True) is RetryVerdict.ESCALATE


def test_test_verdict_broke_build_reverts() -> None:
    # ran before, doesn't run now → strictly worse → revert even though
    # final_failures (0, no FAILED lines) looks "better".
    v = failing_test_verdict(passed=False, initial_failures=2, final_failures=0,
                     initially_ran=True, finally_ran=False, has_snapshot=True)
    assert v is RetryVerdict.REVERT


def test_test_verdict_keeps_partial_and_passes() -> None:
    assert failing_test_verdict(passed=True, initial_failures=2, final_failures=0,
                        initially_ran=True, finally_ran=True, has_snapshot=True) is RetryVerdict.PASS
    assert failing_test_verdict(passed=False, initial_failures=2, final_failures=1,
                        initially_ran=True, finally_ran=True, has_snapshot=True) is RetryVerdict.KEEP_PARTIAL


def test_retry_mode_values() -> None:
    assert RetryMode.BUILD == "build" and RetryMode.TEST == "test"
