"""Process-wide graceful-stop signal.

`sys.exit()` only unwinds the CALLING thread. Under `PIPELINE_PARALLELISM>1`,
fix-loop attempts run inside worker threads (per-fn and per-impl-block worker
pools, and any future parallel dispatch) — a worker thread detecting the stop
sentinel would unlink it, log, and quietly terminate ITSELF, while every other
worker (and the main thread) never saw the file disappear and kept running,
sometimes for hours, since nothing else recorded that a stop was requested.

`_STOP_EVENT` makes the request visible process-wide: once ANY caller sees the
sentinel, every subsequent `check_graceful_stop` call anywhere — including a
worker-pool's own dispatch loop (stop pulling new work) and the pool's owning
code right after joining its workers (the only context where raising
`SystemExit` actually ends the process, since that always runs on the main
thread) — sees the event set and exits too, even though the sentinel file
itself is long gone.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

SENTINEL_PATH = Path("/tmp/pipeline_graceful_stop")

_STOP_EVENT = threading.Event()


def check_graceful_stop(log: Callable[[str], None], context: str) -> None:
    """Exit cleanly when the graceful-stop sentinel is set. Call from inside
    every long-running loop — including worker threads — so a graceful stop
    stays responsive regardless of which thread notices first."""
    if SENTINEL_PATH.exists():
        with suppress(OSError):
            SENTINEL_PATH.unlink()
        _STOP_EVENT.set()
    if _STOP_EVENT.is_set():
        log(f"  [graceful] stop sentinel detected at {context} — exiting cleanly.")
        sys.exit(0)


def reset_for_tests() -> None:
    """Test-only: the event is process-wide state: clear it between cases."""
    _STOP_EVENT.clear()
