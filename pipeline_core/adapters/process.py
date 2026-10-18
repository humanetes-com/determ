"""Bounded, group-killing runner for every process that executes a
reconstructed test binary.

A test binary built from the reconstructed tree is a program the model
wrote: it can abort, loop, or deadlock. `subprocess.run(timeout=)` kills
only the direct child. Cargo (or gdb) is that child; the binary is the
grandchild; a hung grandchild is re-parented to init and keeps waiting
on its futex. Measured 2026-09-08: 27 `deps/*--test-threads=1` processes
with PPID 1, every one `S<l futex_wait`, left by `CargoCli.run`'s 120s
bound (run `3014175-d98ed09e`, `3371694-c8835690`, `3400546-41aeadee`).
The coverage slice's private `_run` had no bound at all (run
`3500465-67c40e78`: pid 3605384, `lib_gen_1`, 89 minutes and counting).

This module is the one primitive those runners share. `start_new_session`
puts the child in its own process group so `killpg` reaches the binary;
`PR_SET_PDEATHSIG` plus a live-pgid table plus SIGTERM/SIGINT/atexit
handlers mean a pipeline SIGTERM does not recreate the orphan class on
the other side of the session detach.

What this deliberately does not run: git, rustc `--print`, llvm-profdata /
llvm-cov, the extractor. They do not execute the reconstructed tree.
"""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.enums import ProcessFailure

__all__ = ["BoundedRun", "cargo_manifest_refusal", "group_members",
           "kill_live_groups", "run_bounded"]

_PR_SET_PDEATHSIG = 1
_LIVE: set[int] = set()
_LIVE_LOCK = threading.Lock()
_HANDLERS_ARMED = False
# What `signal.getsignal` returned before this module took the signal: a
# handler callable, SIG_DFL, SIG_IGN or None. Typed Any because `signal`'s
# own stubs name that union and mypy cannot narrow `object` back to it.
_PREV_HANDLERS: dict[int, Any] = {}


@dataclass(frozen=True, slots=True)
class BoundedRun:
    """A finished (or timed-out) process group.

    ``pgid`` is the session the child led, kept so a test can assert the
    group is empty after return. None when the child never started.
    """

    returncode: int
    stdout: str
    stderr: str
    failure: ProcessFailure | None = None
    pgid: int | None = None


def _partial_text(captured: object) -> str:
    """TimeoutExpired.stdout/.stderr may be bytes or None regardless of
    text=True — normalise to str."""
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return str(captured)


def _child_setup() -> None:
    """In the child, after setsid: die if the pipeline dies.

    PDEATHSIG hits the session leader (cargo, gdb, the test binary). The
    grandchildren still need killpg, which the parent does on timeout and
    on its own exit. The parent-already-dead check closes the race where
    the pipeline dies between fork and prctl.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):
        return
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGKILL)


def group_members(pgid: int) -> list[tuple[int, str]]:
    """``(pid, state)`` of every process in ``pgid`` that is not a zombie.

    Read from ``/proc/<pid>/stat`` (state is field 3, pgid field 5, both
    after the parenthesised comm). A zombie holds no futex and no file;
    it exists only until its reaper calls wait(), which for a killed
    grandchild is init, on init's schedule — so ``killpg(pgid, 0)`` says
    "alive" for a few milliseconds after every member is dead. The
    contract "no process of the group is alive" is about this list.
    """
    out: list[tuple[int, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8",
                      errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        tail = stat.rsplit(")", 1)[-1].split()
        if len(tail) < 3:
            continue
        state, _ppid, grp = tail[0], tail[1], tail[2]
        if grp == str(pgid) and state != "Z":
            out.append((int(name), state))
    return out


def _kill_group(pgid: int, *, drain: float = 2.0) -> None:
    """SIGKILL the group, then wait (at most ``drain`` seconds) until no
    member is left running. The wait is what makes the return a fact
    about the group and not about the signal having been sent."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + drain
    while group_members(pgid) and time.monotonic() < deadline:
        time.sleep(0.005)


def kill_live_groups() -> None:
    """SIGKILL every process group `run_bounded` still has open."""
    with _LIVE_LOCK:
        pgids = list(_LIVE)
        _LIVE.clear()
    for pgid in pgids:
        _kill_group(pgid)


def _on_signal(signum: int, frame: object) -> None:
    kill_live_groups()
    prev = _PREV_HANDLERS.get(signum, signal.SIG_DFL)
    signal.signal(signum, prev if callable(prev) or prev in (
        signal.SIG_DFL, signal.SIG_IGN) else signal.SIG_DFL)
    if callable(prev):
        prev(signum, frame)
        return
    if prev == signal.SIG_IGN:
        return
    os.kill(os.getpid(), signum)


def _arm_handlers() -> None:
    global _HANDLERS_ARMED
    if _HANDLERS_ARMED:
        return
    _HANDLERS_ARMED = True
    atexit.register(kill_live_groups)
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            _PREV_HANDLERS[signum] = signal.getsignal(signum)
            signal.signal(signum, _on_signal)
        except (ValueError, OSError):
            # Not the main thread, or signals unavailable (a test worker).
            pass


def cargo_manifest_refusal(argv: Sequence[str], cwd: Path) -> str | None:
    """Why ``argv`` must not run in ``cwd``, or None when it may.

    Cargo looks for `Cargo.toml` in its working directory and then in every
    ancestor, so a cargo command run in a directory with no manifest of its
    own builds whatever crate sits above it, and reports that crate's errors
    as if they were this tree's. The cluster trees live inside the workspace,
    whose `src/lib.rs` is a one-line shell after strip: on every fresh
    cluster entry the seed's compile gate and the pre-pass heal ran before
    the tree had its manifest, built the shell, and logged `STILL BROKEN …
    unresolved import anyhow::anyhow` on a tree that compiles (replayed
    2026-10-17 on checkpoint `18fb6a03d6`, the entry of run
    `412606-f903acb4`). A command that names its manifest is left alone;
    so is anything that is not cargo (test binaries run through the same
    runners)."""
    if not argv or Path(str(argv[0])).name != "cargo":
        return None
    if any(str(a) == "--manifest-path" or str(a).startswith("--manifest-path=")
           for a in argv):
        return None
    if (Path(cwd) / "Cargo.toml").is_file():
        return None
    return (f"NOT A CRATE ROOT: {cwd} has no Cargo.toml, so cargo would build "
            "an ancestor crate; refused")


def run_bounded(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout: float,
) -> BoundedRun:
    """Run ``cmd`` in a new session; on timeout kill the whole group.

    When this returns after a timeout, no process of the group it started
    is alive. ``failure`` is TIMEOUT, BINARY_MISSING or NO_MANIFEST (a cargo
    command :func:`cargo_manifest_refusal` refuses; nothing is started); a
    normally-exited process (including rc != 0) has ``failure is None``.
    """
    argv = [str(c) for c in cmd]
    refusal = cargo_manifest_refusal(argv, cwd)
    if refusal is not None:
        return BoundedRun(-1, "", refusal, failure=ProcessFailure.NO_MANIFEST)
    _arm_handlers()
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True, preexec_fn=_child_setup)
    except FileNotFoundError as exc:
        return BoundedRun(
            -1, "", f"FILE NOT FOUND: {exc}",
            failure=ProcessFailure.BINARY_MISSING)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    with _LIVE_LOCK:
        _LIVE.add(pgid)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
            return BoundedRun(int(proc.returncode), out or "", err or "",
                              pgid=pgid)
        except subprocess.TimeoutExpired as exc:
            _kill_group(pgid)
            try:
                out, err = proc.communicate()
            except Exception:  # noqa: BLE001 — reap best-effort
                out = _partial_text(exc.stdout)
                err = _partial_text(exc.stderr)
            err = (err or "") + f"\nTIMEOUT after {timeout}s"
            return BoundedRun(
                -1, out or "", err,
                failure=ProcessFailure.TIMEOUT, pgid=pgid)
        except Exception:
            _kill_group(pgid)
            raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(pgid)
        if proc.poll() is None:
            _kill_group(pgid)
            with contextlib.suppress(Exception):  # reap best-effort
                proc.communicate()
