"""Retry util — call-until-success with rate-limit + network backoff.

Relocated from pipeline.py (slice 5b infra foundation). Bodies verbatim; the former module
globals are now module config set once by the composition root via :func:`configure`
(``MAX_RETRIES``/``RATE_LIMIT_WAIT``/``MODEL``), and logging goes through the
:mod:`pipeline_core.log` facade. Generic infra: any layer may import ``with_retry`` instead
of injecting a ``retry`` collaborator.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import log as _log

# Module config (overridden by configure() at the composition root).
MAX_RETRIES: int = 3
RATE_LIMIT_WAIT: int = 60  # seconds to wait when hitting a rate limit
MODEL: str = ""


def configure(*, max_retries: int, rate_limit_wait: int, model: str) -> None:
    """Set the retry config from the resolved pipeline settings. Called once at startup."""
    global MAX_RETRIES, RATE_LIMIT_WAIT, MODEL
    MAX_RETRIES = max_retries
    RATE_LIMIT_WAIT = rate_limit_wait
    MODEL = model


def is_rate_limit_error(e: BaseException) -> bool:
    """Return True (and sleep) if e is a provider rate-limit error."""
    if MODEL.startswith("claude"):
        try:
            import anthropic
            if isinstance(e, anthropic.RateLimitError):
                _log.log(f"  Rate limit hit: {e}. Waiting {RATE_LIMIT_WAIT}s before retrying...")
                time.sleep(RATE_LIMIT_WAIT)
                return True
        except ImportError:
            pass
    return False


def with_retry[RT](fn: Callable[[], RT], retries: int | None = None) -> RT:
    """
    Call fn() up to `retries` times. fn must return False to signal failure,
    or any non-False value to signal success.
    Rate limit errors (429) pause for RATE_LIMIT_WAIT seconds before retrying
    and do not consume a retry attempt.
    """
    if retries is None:
        retries = MAX_RETRIES
    attempt = 1
    while attempt <= retries:
        try:
            result = fn()
        except Exception as e:
            if is_rate_limit_error(e):
                continue  # retry without consuming an attempt
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
