"""Wall-clock bound on one TDD fill phase (file-first + impl + per-fn).

``stall`` is written only when ``cargo test`` finishes an iteration, and a
mid-pass fill is designed not to charge. Run ``463713-b7c666c8`` therefore
printed ``stall=0/0`` across eight hours inside iter 2. This clock is the
stop that can see that class. ``0`` (the default) disables it.

Minutes are read once at cluster entry (``configured_minutes``) and passed
to ``arm`` each fill — not re-read from the env per iteration, the same
rule as the other stop knobs. ``poll`` is called from ``check_graceful_stop``
so every worker loop already polling the sentinel also polls this bound.
First expiry logs and sets the process-wide stop event via the caller —
this module does not import ``graceful_stop`` (cycle).
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

_lock = threading.Lock()
_deadline: float | None = None
_tripped = False
_log: Callable[[str], None] | None = None
_emit: Callable[..., None] | None = None
_meta: dict[str, str | int] = {}


def arm(
    max_minutes: int,
    *,
    log: Callable[[str], None] | None = None,
    emit: Callable[..., None] | None = None,
    cluster: str = "",
    iter_n: int = 0,
) -> None:
    """Start (or disable) the fill-phase clock. ``max_minutes <= 0`` disarms."""
    global _deadline, _tripped, _log, _emit, _meta
    with _lock:
        _tripped = False
        _log = log
        _emit = emit
        _meta = {"cluster": cluster, "iter": iter_n, "max_minutes": max_minutes}
        if max_minutes <= 0:
            _deadline = None
            return
        _deadline = time.monotonic() + max_minutes * 60.0


def configured_minutes() -> int:
    """``TDD_FILL_PHASE_MAX_MINUTES`` as an int; ``0`` on unset or garbage."""
    raw = os.getenv("TDD_FILL_PHASE_MAX_MINUTES", "0").strip() or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def arm_from_env(
    *,
    log: Callable[[str], None] | None = None,
    emit: Callable[..., None] | None = None,
    cluster: str = "",
    iter_n: int = 0,
) -> int:
    """Read ``TDD_FILL_PHASE_MAX_MINUTES`` (default ``0``) and :func:`arm`.

    The TDD driver does not use this: it reads the knob once at cluster
    entry and passes that value to :func:`arm` each fill.
    """
    minutes = configured_minutes()
    arm(minutes, log=log, emit=emit, cluster=cluster, iter_n=iter_n)
    return minutes


def disarm() -> None:
    """Test / next-iter: no bound in force."""
    arm(0)


def was_tripped() -> bool:
    """True after the first expired :func:`poll` until the next :func:`arm`."""
    with _lock:
        return _tripped


def poll() -> bool:
    """True once, on the first call after the bound expires.

    The caller (``check_graceful_stop``) then requests the process-wide
    stop. Subsequent polls return False so the log/event fire once.
    """
    global _tripped
    with _lock:
        if _deadline is None or _tripped:
            return False
        if time.monotonic() < _deadline:
            return False
        _tripped = True
        mins = _meta.get("max_minutes", 0)
        ctx = _meta.get("cluster", "")
        iter_n = _meta.get("iter", 0)
        log = _log
        emit = _emit
        payload = dict(_meta)
    line = (
        f"  FILL_PHASE_STOP fill phase exceeded TDD_FILL_PHASE_MAX_MINUTES="
        f"{mins} at cluster={ctx} iter={iter_n} — exiting cleanly."
    )
    if log is not None:
        log(line)
    if emit is not None:
        emit("phase_c.tdd.fill_phase_stop", data=payload)
    return True
