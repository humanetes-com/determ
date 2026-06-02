"""Logging seam — an ambient, settable log/emit facade (the stdlib-`logging` pattern).

Cross-cutting concern: any layer may import this. The composition root calls
:func:`set_sinks` once to point ``log``/``emit`` at the pipeline's real sinks
(``_log``/``_emit``); until then ``log`` prints and ``emit`` is a no-op. Lower-layer
helpers (retry, and later the extracted tasks) call ``log``/``emit`` here instead of
taking a logger in every deps record — so the facade replaces per-call logger injection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_log_sink: Callable[[str], None] = print
_emit_sink: Callable[..., None] = lambda event, **fields: None  # noqa: E731


def set_sinks(log_fn: Callable[[str], None], emit_fn: Callable[..., None]) -> None:
    """Wire the facade to concrete sinks. Called once at the composition root."""
    global _log_sink, _emit_sink
    _log_sink = log_fn
    _emit_sink = emit_fn


def log(msg: str) -> None:
    _log_sink(msg)


def emit(event: str, **fields: Any) -> None:
    _emit_sink(event, **fields)
