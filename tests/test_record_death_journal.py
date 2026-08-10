"""The death recorder's write-ahead birth journal.

The exit-time death row cannot report a death that takes the recorder too:
on 2026-08-09 run 1159463 (the 18->76 climb) vanished with its supervisor
between 08:41 and the 22:44 relaunch, and deaths.jsonl still ended at the
PREVIOUS run — KILL is untrappable, so the forwarding traps never ran. The
journal closes the hole: a birth row lands before the child starts, and the
next launch synthesizes a death row for any birth that has no matching death
and whose processes are gone.

These tests drive the real script with a throwaway child. The pid-file path
is overridden so a test run never clobbers the live pipeline's
/tmp/determ_pipeline.pid.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "lib" / "record_death.sh"


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "DETERM_PIPELINE_PIDFILE": str(tmp_path / "pid")}


def _wait_for(predicate, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached within timeout")


def _launch(log_dir: Path, env: dict[str, str], *child: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["bash", str(_SCRIPT), str(log_dir), *child],
        env=env, cwd=str(_SCRIPT.parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _kill_both(supervisor: subprocess.Popen, child_pid: int) -> None:
    """The hole under test: KILL both processes at once, traps never run."""
    for pid in (supervisor.pid, child_pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    supervisor.wait(timeout=5)


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _orphan(tmp_path: Path) -> tuple[Path, dict[str, str], int]:
    """Start a run, KILL supervisor+child together, return the orphan pid."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    env = _env(tmp_path)
    sup = _launch(log_dir, env, "bash", "-c", "sleep 30")
    pid_file = tmp_path / "pid"
    _wait_for(pid_file.exists)
    child_pid = int(pid_file.read_text().strip())
    _kill_both(sup, child_pid)
    return log_dir, env, child_pid


def test_killed_with_supervisor_is_reconciled_at_the_next_launch(tmp_path):
    """NEGATIVE CONTROL against the pre-journal recorder: after a KILL of
    supervisor+child the next launch must synthesize exactly one death row
    for the orphaned birth. Pre-fix the next launch records only its own
    death and the vanished run stays unexplained — this test then fails."""
    log_dir, env, child_pid = _orphan(tmp_path)
    deaths = log_dir / "deaths.jsonl"
    assert not any(r.get("pid") == child_pid for r in _rows(deaths)), (
        "the KILL itself must leave no row — that is the hole")

    subprocess.run(
        ["bash", str(_SCRIPT), str(log_dir), "bash", "-c", "true"],
        env=env, cwd=str(_SCRIPT.parent.parent), timeout=15,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    synthesized = [r for r in _rows(deaths) if r.get("pid") == child_pid]
    assert len(synthesized) == 1, (
        f"expected exactly one synthesized row for orphan pid {child_pid}; "
        f"deaths.jsonl holds {_rows(deaths)}")
    assert synthesized[0]["signal"] == "UNKNOWN_KILLED_WITH_SUPERVISOR"
    assert synthesized[0].get("synthesized") is True


def test_reconciliation_is_idempotent_across_launches(tmp_path):
    log_dir, env, child_pid = _orphan(tmp_path)
    for _ in range(2):
        subprocess.run(
            ["bash", str(_SCRIPT), str(log_dir), "bash", "-c", "true"],
            env=env, cwd=str(_SCRIPT.parent.parent), timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    synthesized = [r for r in _rows(log_dir / "deaths.jsonl")
                   if r.get("pid") == child_pid]
    assert len(synthesized) == 1, "a birth must be settled exactly once"


def test_a_live_run_is_not_reconciled(tmp_path):
    """A birth whose processes are still alive is a running pipeline, not an
    unexplained death — the reconciler must leave it open."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = _env(tmp_path)
    sup = _launch(log_dir, env, "bash", "-c", "sleep 30")
    pid_file = tmp_path / "pid"
    _wait_for(pid_file.exists)
    child_pid = int(pid_file.read_text().strip())
    try:
        subprocess.run(
            ["bash", str(_SCRIPT), str(log_dir), "bash", "-c", "true"],
            env=env, cwd=str(_SCRIPT.parent.parent), timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert not any(r.get("pid") == child_pid
                       for r in _rows(log_dir / "deaths.jsonl"))
        assert any(r.get("child_pid") == child_pid
                   for r in _rows(log_dir / "births.jsonl")), (
            "the live birth must survive the reconciler's rewrite")
    finally:
        _kill_both(sup, child_pid)


def test_a_normal_exit_still_writes_one_death_row_and_closes_its_birth(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env = _env(tmp_path)
    subprocess.run(
        ["bash", str(_SCRIPT), str(log_dir), "bash", "-c", "exit 7"],
        env=env, cwd=str(_SCRIPT.parent.parent), timeout=15,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows = _rows(log_dir / "deaths.jsonl")
    assert len(rows) == 1 and rows[0]["exit_status"] == 7
    # The next launch must not resurrect the settled birth as an orphan.
    subprocess.run(
        ["bash", str(_SCRIPT), str(log_dir), "bash", "-c", "true"],
        env=env, cwd=str(_SCRIPT.parent.parent), timeout=15,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert not any(r.get("synthesized") for r in _rows(log_dir / "deaths.jsonl"))
