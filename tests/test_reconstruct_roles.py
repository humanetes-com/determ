"""Focused reconstruction role adapters — delegation + the one piece of
real logic (FixerAdapter.diagnose_no_tests)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.reconstruct_roles import (
    FixerAdapter,
    PhaseGateAdapter,
)


def test_phase_gate_forwards_commit_and_mark() -> None:
    seen: dict = {}
    gate = PhaseGateAdapter(
        phase_valid_fn=lambda p, k: True,
        commit_and_mark_fn=lambda *a, **k: seen.update(a=a, k=k),
    )
    assert gate.phase_valid(Path("/p"), "task3") is True
    gate.commit_and_mark(Path("/p"), "task3", "done", commit_message="m", tag="t")
    assert seen["a"] == (Path("/p"), "task3", "done")
    assert seen["k"] == {"commit_message": "m", "tag": "t", "detail": "", "source_hash": None}


def _fixer(no_run_cmd: str) -> FixerAdapter:
    return FixerAdapter(
        fix_loop_fn=lambda *a, **k: (True, ""),
        classify_fn=lambda out: "build",
        log=lambda _m: None,
        insert_no_run=lambda _cmd: no_run_cmd,
        fix_err_cap=200,
    )


def test_diagnose_no_tests_build_error_when_no_run_fails(tmp_path: Path) -> None:
    # `false` exits non-zero → diagnosed as a build error.
    ft, out = _fixer("false").diagnose_no_tests(tmp_path, "cargo test", "0 ran", "ORIG")
    assert ft == "build"
    assert out.startswith("ORIG") and "DIAGNOSIS (--no-run): build failed" in out


def test_diagnose_no_tests_keeps_no_tests_when_no_run_ok(tmp_path: Path) -> None:
    # `true` exits zero → genuinely a no-tests/harness situation, not a build error.
    ft, out = _fixer("true").diagnose_no_tests(tmp_path, "cargo test", "0 ran", "ORIG")
    assert ft == "no_tests"
    assert out == "ORIG\n\nDIAGNOSIS: 0 ran"


def test_classify_delegates() -> None:
    assert _fixer("true").classify_failure("whatever") == "build"
