"""Which route run is executing, for records that have no argument for it.

The fill reading (`phase_c.fill.reading`) is published from inside the
shared regression gate, which every route calls and none tells which run it
is serving; the reading therefore carried pass counts and no route, unit or
cluster, and a route ledger could not join a reading to the run that paid
for it. The executor binds these variables around each run; a publisher
reads them. ContextVars, so a worker thread of a pooled route inherits the
binding of the run that spawned it and a nested binding unwinds itself.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["current_fill", "current_route", "route_scope"]

_ROUTE: ContextVar[str] = ContextVar("fill_route", default="")
_LABEL: ContextVar[str] = ContextVar("fill_run_label", default="")
_CLUSTER: ContextVar[str] = ContextVar("fill_cluster", default="")
_ITER: ContextVar[int] = ContextVar("fill_iter", default=0)


def current_route() -> tuple[str, str]:
    """(route kind, run label) of the run executing now; ("", "") outside."""
    return _ROUTE.get(), _LABEL.get()


def current_fill() -> tuple[str, int]:
    """(cluster, iteration) of the fill executing now; ("", 0) outside."""
    return _CLUSTER.get(), _ITER.get()


@contextmanager
def route_scope(kind: str, label: str, *, cluster: str = "",
                iteration: int = 0) -> Iterator[None]:
    t_route = _ROUTE.set(kind)
    t_label = _LABEL.set(label)
    t_cluster = _CLUSTER.set(cluster) if cluster else None
    t_iter = _ITER.set(iteration) if iteration else None
    try:
        yield
    finally:
        _ROUTE.reset(t_route)
        _LABEL.reset(t_label)
        if t_cluster is not None:
            _CLUSTER.reset(t_cluster)
        if t_iter is not None:
            _ITER.reset(t_iter)
