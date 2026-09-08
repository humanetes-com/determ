"""Timeout paths must keep the partial subprocess output — for a hung test it
names WHICH test was running; discarding it is a feedback-fidelity loss (the
class the starvation miner tags error_truncated)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline_core.adapters.cargo import CargoCli, run_build_check
from pipeline_core.adapters.process import BoundedRun
from pipeline_core.domain.enums import ProcessFailure


def _raise_timeout(*a, **k):
    raise subprocess.TimeoutExpired(
        cmd="cargo test", timeout=5,
        output=b"running 3 tests\ntest test_hang ... ",
        stderr=b"warning: something\n")


def _timeout_run(*a, **k):
    return BoundedRun(
        -1, "running 3 tests\ntest test_hang ... ",
        "warning: something\nTIMEOUT after 5s",
        failure=ProcessFailure.TIMEOUT)


def test_cargocli_run_keeps_partial_output_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr("pipeline_core.adapters.cargo.run_bounded", _timeout_run)
    p = CargoCli().run("cargo test", Path("."), env={}, timeout=5)
    assert p.returncode == -1
    assert "test_hang" in p.stdout
    assert "TIMEOUT after 5s" in p.stderr


def test_run_build_check_keeps_partial_output_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    ok, out = run_build_check(Path("."), "cargo check",
                              cargo_env=dict, build_timeout=5)
    assert ok is False
    assert "test_hang" in out and "timed out" in out


def test_process_failure_class_is_explicit(monkeypatch) -> None:
    """rc=-1 conflation resolved: TIMEOUT vs BINARY_MISSING are distinct,
    typed, and the tuple view keeps the partial stdout."""
    from pipeline_core.application.reconstruct.cargo_runner import run_cargo

    monkeypatch.setattr("pipeline_core.adapters.cargo.run_bounded", _timeout_run)
    cli = CargoCli()
    p = cli.run("cargo test", Path("."), env={}, timeout=5)
    assert p.failure is ProcessFailure.TIMEOUT
    rc, out = run_cargo(cli, "cargo test", Path("."), {}, timeout=5)
    assert rc == -1 and "test_hang" in out and "TIMEOUT" in out

    def _missing(*a, **k):
        return BoundedRun(-1, "", "FILE NOT FOUND: no cargo",
                          failure=ProcessFailure.BINARY_MISSING)
    monkeypatch.setattr("pipeline_core.adapters.cargo.run_bounded", _missing)
    p = cli.run("cargo test", Path("."), env={}, timeout=5)
    assert p.failure is ProcessFailure.BINARY_MISSING
