"""Retry util — call-until-success with network backoff.

Relocated from pipeline.py (slice 5b infra foundation). Bodies verbatim; the former module
global is now module config set once by the composition root via :func:`configure`
(``MAX_RETRIES``), and logging goes through the :mod:`pipeline_core.log` facade. Generic
infra: any layer may import ``with_retry`` instead of injecting a ``retry`` collaborator.

Self-hosted only (Hard Rule 7): there is no external/cloud provider, so there is no
provider rate-limit path — only local network-error backoff.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import log as _log

# Module config (overridden by configure() at the composition root).
MAX_RETRIES: int = 3


def configure(*, max_retries: int) -> None:
    """Set the retry config from the resolved pipeline settings. Called once at startup."""
    global MAX_RETRIES
    MAX_RETRIES = max_retries


def with_retry[RT](fn: Callable[[], RT], retries: int | None = None) -> RT:
    """
    Call fn() up to `retries` times. fn must return False to signal failure,
    or any non-False value to signal success.
    Transient network errors pause briefly before retrying.
    """
    if retries is None:
        retries = MAX_RETRIES
    attempt = 1
    while attempt <= retries:
        try:
            result = fn()
        except Exception as e:
            _ename = type(e).__name__
            if any(t in _ename for t in ("Timeout", "ConnectionError", "APIError", "ReadTimeout")):
                _wait = min(30, 5 * attempt)
                _log.log(f"  Network error ({_ename}) — waiting {_wait}s before retry ({attempt}/{retries})...")
                time.sleep(_wait)
                attempt += 1
                continue
            raise
        if result is not False:
            return result
        if attempt < retries:
            _log.log(f"  Attempt {attempt}/{retries} failed, retrying...")
        attempt += 1
    _log.log(f"  All {retries} attempts failed.")
    return False  # type: ignore[return-value]
