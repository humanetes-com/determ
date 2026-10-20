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

from pipeline_core.domain import fill_phase_clock, reasoning_cap
from pipeline_core.domain.runtime_paths import GRACEFUL_STOP, control_path


def sentinel_path() -> Path:
    """The stop sentinel `touch`ed by the monitor and `lib/graceful_stop.sh`
    (resolved per call, so a redirected control dir is honoured)."""
    return control_path(GRACEFUL_STOP)

_STOP_EVENT = threading.Event()


def stop_requested() -> bool:
    """Whether the process has been asked to stop, by any of the three
    sources (the sentinel, the fill-phase clock, the LLM cut rate), without
    exiting. Noticing a source sets the process-wide event, exactly as
    :func:`check_graceful_stop` does.

    A unit in flight reads this at its call boundaries
    (`unit_clock.ended`) so that it ends through its route's own exit, not
    through `SystemExit` from inside its fix loop. That exit is where a
    banked better image is written back. Run `3495074-71c576c7`
    (2026-10-20) measured `pass 94 -> 223 (net +129) … no regression` on an
    impl attempt for `Drop for Error`, banked it as salvage, and the stop
    raised at the next attempt's boundary; the block's exit never ran, and
    the tree went back to the stub at 94."""
    sentinel = sentinel_path()
    if sentinel.exists():
        with suppress(OSError):
            sentinel.unlink()
        _STOP_EVENT.set()
    # Wall-clock bound on one TDD fill phase. Armed from the TDD driver;
    # ``0`` leaves the clock idle so this is a no-op. First expiry logs
    # ``FILL_PHASE_STOP`` (inside poll) then takes the same process-wide
    # event a sentinel stop takes — no leftover file for the next launch.
    if fill_phase_clock.poll():
        _STOP_EVENT.set()
    # The run-level LLM cut rate (`reasoning_cap.CutRate`): the fold already
    # logged `HEALTH ‼ LLM_CUT_STOP` and emitted its event when it fired.
    if reasoning_cap.cut_stop_poll():
        _STOP_EVENT.set()
    return _STOP_EVENT.is_set()


def check_graceful_stop(log: Callable[[str], None], context: str) -> None:
    """Exit cleanly when the graceful-stop sentinel is set. Call from inside
    every long-running loop — including worker threads — so a graceful stop
    stays responsive regardless of which thread notices first."""
    if stop_requested():
        if reasoning_cap.cut_stop_tripped():
            log(f"  [graceful] LLM cut-rate stop at {context} — exiting cleanly.")
        elif fill_phase_clock.was_tripped():
            log(f"  [graceful] fill-phase bound at {context} — exiting cleanly.")
        else:
            log(f"  [graceful] stop sentinel detected at {context} — exiting cleanly.")
        sys.exit(0)


def request_graceful_stop() -> None:
    """Ask for the stop an operator's sentinel asks for, from INSIDE the run.

    A mechanism that has decided the run should end (the flat-score GPU guard in
    the TDD driver is the first) must not call `sys.exit` where it stands: it
    runs at a decision point that has bookkeeping left to do — the cluster's
    checkpoint, its final sweep, the restore of an abandoned bet — and under
    `PIPELINE_PARALLELISM>1` an exit on one thread ends only that thread. Set the
    process-wide event instead and return; the caller finishes its own exit path,
    and the next `check_graceful_stop` anywhere ends the process cleanly with the
    same code, the same log line and the same worker-pool behaviour a sentinel
    stop gets. No file is written, so nothing is left behind to stop the NEXT
    run: the sentinel path is unchanged and this one is process-local."""
    _STOP_EVENT.set()


def reset_for_tests() -> None:
    """Test-only: the event is process-wide state: clear it between cases."""
    _STOP_EVENT.clear()
    fill_phase_clock.disarm()
    reasoning_cap.reset_cut_stop_for_tests()
