"""Threads that continue the context of the code that spawned them.

Cross-cutting like :mod:`pipeline_core.log`: any layer may import it.

A plain `threading.Thread` or `ThreadPoolExecutor` worker starts on an EMPTY
`contextvars` context. Every ContextVar the pipeline binds around work is then
silently unset inside the worker: the route a fill is executing
(`domain/fill_context.py`), the observer collecting a call's stream rows, and
the phase scope (:func:`pipeline_core.phases.current_scope`) that the event
emitter stamps on every event so a phase's LLM calls and cargo runs are
charged to it. A pooled lane's calls came back unlabelled, and per-phase cost
read zero on exactly the phases that run in parallel.

Each worker gets its OWN copy of the spawning context: one `Context` may not
be entered from two threads at once, and a binding a worker makes stays in
that worker. `tests/test_threads.py` fails on a pipeline module that builds a
bare `threading.Thread` or `ThreadPoolExecutor` instead of these two.
"""
from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

__all__ = ["ContextPool", "context_thread"]


def context_thread(target: Callable[..., object], *, name: str,
                   args: tuple[Any, ...] = (), daemon: bool | None = None
                   ) -> threading.Thread:
    """A `threading.Thread` that runs `target(*args)` inside a copy of the
    caller's context, taken now (at construction, on the spawning thread)."""
    ctx = contextvars.copy_context()

    def _run() -> None:
        ctx.run(target, *args)

    return threading.Thread(target=_run, name=name, daemon=daemon)


class ContextPool(ThreadPoolExecutor):
    """A `ThreadPoolExecutor` whose every task runs inside a copy of the
    context that submitted it (so `map` is covered too: it submits)."""

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args,
                       **kwargs: P.kwargs) -> Future[T]:
        ctx = contextvars.copy_context()

        def _run() -> T:
            return ctx.run(fn, *args, **kwargs)

        return super().submit(_run)
