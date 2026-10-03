"""Auto-operator adapter — sentinel-driven, unattended stall-triggered operator
pass. Runs inside the pipeline process itself (not the monitor) so it stays
live whether or not the monitor is attached.

Mirrors `pipeline_core.adapters.power`'s SHUTDOWN_ON_FINISH sentinel: the
monitor's `[a]` toggle creates/removes the AUTOMODE control channel
(`domain.runtime_paths`) — presence means auto
mode is ON, absence means OFF (same touch/remove convention as
`domain.graceful_stop`'s stop sentinel). A fresh sentinel-less pipeline start
falls back to the `.env` `AUTO_DEFAULT` setting; `seed_auto_operator_default`
materialises that default into the sentinel once at startup so auto mode
stays live-toggleable (and inspectable by the monitor) from that point on.

Firing is delegated to the pure `AutoOperator` state machine (domain layer) so
the fire-once-per-episode / no-overlap rules stay unit-tested without a
subprocess; this module supplies the I/O: sentinel read, `.env` fallback, and
the detached `operator_run.sh` launch.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pipeline_core.domain.auto_operator import AutoOperator
from pipeline_core.domain.runtime_paths import (
    AUTOMODE,
    OPERATOR_LAST_FIRE,
    OPERATOR_PASS_LOCK,
    control_path,
)

# Machine-global pass identity. The in-process `_OP` handle can only see passes
# THIS process launched, and an operator pass routinely kills and restarts the
# pipeline — so the restarted process starts armed with an empty handle and
# would happily launch a second, concurrent pass over the same working tree.
# `operator_run.sh` holds an exclusive flock on this file for its whole run;
# both files (OPERATOR_PASS_LOCK, OPERATOR_LAST_FIRE under the control dir)
# outlive any pipeline process, which is the point.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_LOCK = threading.Lock()
_OP = AutoOperator(trigger=int(os.getenv("AUTO_STALL_TRIGGER", "1")))


def auto_operator_enabled(sentinel: Path | None = None) -> bool:
    """Runtime authority: sentinel presence overrides the `.env` default."""
    if (sentinel or control_path(AUTOMODE)).exists():
        return True
    return os.getenv("AUTO_DEFAULT", "0").strip() == "1"


def seed_auto_operator_default(sentinel: Path | None = None) -> None:
    """Startup-only: make the `.env` `AUTO_DEFAULT` the authority again, so auto
    mode is ON from the first TDD iteration without requiring the monitor to be
    open (mirrors why SHUTDOWN_ON_FINISH is read from `.env`, not re-derived).
    Applied in BOTH directions: the sentinel lives in the control dir and survives every
    pipeline restart, so a presence-only file that is never cleared would leave
    an unattended, billed operator pass armed forever after one monitor toggle
    — a fresh start with the default off must fail off."""
    sentinel = sentinel or control_path(AUTOMODE)
    if os.getenv("AUTO_DEFAULT", "0").strip() == "1":
        sentinel.touch()
    else:
        with contextlib.suppress(OSError):
            sentinel.unlink()


def _pass_running(lock: Path | None = None) -> bool:
    """True while an `operator_run.sh` pass holds the machine-global flock —
    including one launched by a PREVIOUS pipeline process that this pass itself
    killed and restarted."""
    lock = lock or control_path(OPERATOR_PASS_LOCK)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _cooling_down(last_fire: Path | None = None, now: float | None = None) -> bool:
    """Disk-persisted refractory period between passes. `AutoOperator.armed`
    only survives inside one process, but a pass restarts the pipeline, so the
    restarted process re-arms with a fresh stall counter and fires again the
    moment the plateau reappears — an unbounded chain of billed passes on a
    crate the operator cannot fix. The timestamp is what carries the
    fire-once-per-episode rule across those restarts."""
    cooldown = float(os.getenv("AUTO_COOLDOWN_SEC", "7200"))
    try:
        last = float((last_fire or control_path(OPERATOR_LAST_FIRE)).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (now if now is not None else time.time()) - last < cooldown


def _record_fire(last_fire: Path | None = None) -> None:
    with contextlib.suppress(OSError):
        (last_fire or control_path(OPERATOR_LAST_FIRE)).write_text(f"{time.time():.0f}\n", encoding="utf-8")


def _launch_operator_pass() -> subprocess.Popen[bytes]:
    """Detached, session-leader launch — mirrors monitor.py's `_detach`, so
    the pass outlives this process even if the operator kills/restarts it."""
    return subprocess.Popen(
        ["bash", str(_REPO_ROOT / "operator_run.sh")], cwd=_REPO_ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def check_auto_operator(log: Callable[[str], None], stall: int, context: str, *,
                        sentinel: Path | None = None) -> None:
    """Call once per TDD iteration with the cluster's current stall count.
    Process-wide + lock-guarded so concurrent cluster workers under
    PIPELINE_PARALLELISM>1 never double-fire for the same stall episode; the
    flock probe and the cooldown extend that same guarantee across the pipeline
    restart an operator pass performs on its way out."""
    with _LOCK:
        enabled = (auto_operator_enabled(sentinel)
                   and not _pass_running() and not _cooling_down())
        if _OP.tick(enabled, stall, _launch_operator_pass):
            _record_fire()
            log(f"  [auto-operator] stall={stall}>={_OP.trigger} at {context} — "
                "launching one operator_run.sh pass (see logs/auto/operator_run.log)")
