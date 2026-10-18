"""run_bounded kills the process group; subprocess.run's timeout does not.

The orphan of a cargo timeout (run 3500465-67c40e78 and the 27 PPID-1
`deps/*--test-threads=1` binaries on 2026-09-08) is a grandchild the
direct-child kill misses. This is that orphan in 300 ms.

"Alive" is a RUNNING member of the group, read from /proc. A killed
grandchild is a zombie until init reaps it, on init's schedule, and
`killpg(pgid, 0)` still succeeds on a zombie — the first version of this
test asserted on that call and flaked 1 in 3 on a loaded box.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from pathlib import Path

from pipeline_core.adapters.process import group_members, run_bounded
from pipeline_core.domain.enums import ProcessFailure

_CMD = ["bash", "-c", "sleep 60 & exec sleep 60"]


def test_run_bounded_kills_the_group_and_plain_run_leaves_the_background() -> None:
    r = run_bounded(_CMD, cwd=Path("."), env=os.environ, timeout=0.3)
    assert r.failure is ProcessFailure.TIMEOUT
    assert r.returncode == -1
    assert "TIMEOUT after" in r.stderr
    assert r.pgid is not None
    # The contract: when run_bounded returns after a timeout, no process of
    # the group is running — the backgrounded sleep included.
    assert group_members(r.pgid) == []

    # Negative control — the pre-fix runner's shape: a direct-child kill
    # (what `subprocess.run(timeout=)` does) leaves the backgrounded sleep
    # running in the group.
    leftover = subprocess.Popen(
        _CMD, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pgid = os.getpgid(leftover.pid)
    try:
        try:
            leftover.communicate(timeout=0.3)
        except subprocess.TimeoutExpired:
            leftover.kill()
            leftover.communicate()
        running = group_members(pgid)
        assert running, "the direct-child kill should have left the grandchild"
        assert all(state != "Z" for _pid, state in running)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)


def test_a_cargo_command_in_a_directory_with_no_manifest_is_refused(
        tmp_path: Path) -> None:
    """Cargo climbs to an ancestor manifest; the cluster trees sit inside the
    workspace crate, so a tree checked before it had its own `Cargo.toml`
    built the stripped workspace shell (run `412606-f903acb4`'s entry,
    replayed 2026-10-17). Nothing is started."""
    from pipeline_core.adapters.process import cargo_manifest_refusal
    r = run_bounded(["cargo", "check"], cwd=tmp_path, env=None, timeout=5)
    assert r.returncode == -1 and r.failure is ProcessFailure.NO_MANIFEST
    assert r.pgid is None and "NOT A CRATE ROOT" in r.stderr
    assert cargo_manifest_refusal(["/usr/bin/cargo", "+nightly", "test"], tmp_path)
    # Not refused: a manifest here, a named manifest, a program that is not cargo.
    assert cargo_manifest_refusal(
        ["cargo", "check", "--manifest-path", "x/Cargo.toml"], tmp_path) is None
    assert cargo_manifest_refusal(["./target/debug/deps/t-1"], tmp_path) is None
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert cargo_manifest_refusal(["cargo", "check"], tmp_path) is None
