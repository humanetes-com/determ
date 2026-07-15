"""Power-off-on-finish — the run-end poweroff hook and its sentinel authority.

The monitor's [p] toggle writes "1"/"0" to the sentinel; that overrides the
.env SHUTDOWN_ON_FINISH default. /tmp clears on reboot → back to .env.
(monitor.py mirrors the sentinel read for its status line.)"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

SHUTDOWN_SENTINEL_PATH = Path("/tmp/pipeline_shutdown_on_finish")


def shutdown_on_finish_enabled(sentinel: Path = SHUTDOWN_SENTINEL_PATH) -> bool:
    """Runtime authority: sentinel file ("1"/"0") overrides the .env default."""
    try:
        return sentinel.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return os.getenv("SHUTDOWN_ON_FINISH", "0").strip() == "1"


def poweroff_on_finish(log: Callable[[str], None], *,
                       sentinel: Path = SHUTDOWN_SENTINEL_PATH) -> None:
    """Power off when the run ends on its own (any sys.exit or crash). Not on
    Ctrl-C; not on SIGTERM (restart.sh kill terminates the process before the
    finally block runs). Gated by the sentinel/.env authority so it stays
    live-toggleable from the monitor."""
    if not shutdown_on_finish_enabled(sentinel):
        return
    log("shutdown-on-finish enabled — powering off (sudo -n systemctl poweroff).")
    subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=False)
