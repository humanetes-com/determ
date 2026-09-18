"""Logging seam — an ambient, settable log/emit facade (the stdlib-`logging` pattern).

Cross-cutting concern: any layer may import this. The composition root calls
:func:`set_sinks` once to point ``log``/``emit`` at the pipeline's real sinks
(``_log``/``_emit``); until then ``log`` prints and ``emit`` is a no-op. Lower-layer
helpers (retry, and later the extracted tasks) call ``log``/``emit`` here instead of
taking a logger in every deps record — so the facade replaces per-call logger injection.
"""

from __future__ import annotations

import sys
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


def caller_site(depth: int = 2) -> str:
    """`qualname (file:lineno)` of the frame `depth` levels up — used to tag
    llm.request with WHO issued it, so a C10 same-thread overlap can be traced
    to the concurrent/reentrant call site instead of guessed."""
    try:
        f = sys._getframe(depth)
        return f"{f.f_code.co_qualname} ({f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno})"
    except Exception:
        return "?"
