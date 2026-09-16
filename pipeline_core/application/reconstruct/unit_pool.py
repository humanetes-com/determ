"""Run independent work units over a fixed thread pool.

The fill routes all have the same shape: a list of units that touch disjoint
files, a slow LLM call inside each, and shared state already serialised by the
splice/counter locks — so units may run concurrently and a unit's own failure
must not take its neighbours with it.

It existed twice (the per-fn engine's level worker, the per-impl route's
by-file worker) and the third caller was written as a plain `for` loop on the
main thread. That loop is where run `491052-1af6b4f4`'s wall clock went: paired
from `logs/events.jsonl`, 47 % of its 5-second slots had a MainThread call in
flight, and across those slots mean concurrency was **1.03** against 2.85 while
MainThread was free — the worker pools idled behind a serial loop.

Deliberately NOT a general executor. Three properties the fill routes depend on
and a bare `ThreadPoolExecutor` does not give:

* a unit is indivisible and one worker owns it start to finish (sibling impls
  on one type are co-designed, so they must not be split across workers);
* `check_stop` is polled before each unit is claimed, so a graceful stop drains
  rather than aborts — and it is re-raised on the CALLING thread after the
  join, because `sys.exit()` in a worker kills that worker alone;
* a unit that raises is logged and skipped; the pool keeps going.

A fourth: a worker CONTINUES the context of the thread that spawned it. A
plain `threading.Thread` starts on an empty context, so every ContextVar the
fill binds around a route run — which route is executing
(`domain/fill_context.py`, whose docstring already states the inheritance as
a fact), which units a call serves, which observer collects that call's
stream rows — was silently unset inside a pooled route's workers, and the
records keyed on them came back unlabelled. Each worker gets its OWN copy of
the spawning context (`contextvars.copy_context()` per thread, never one
shared `Context` — a Context may not be entered from two threads at once),
so a binding made inside a worker still scopes that worker alone.
"""
from __future__ import annotations

import contextvars
import queue as _queue_mod
import threading
from collections.abc import Callable, Iterable


def run_units_concurrently[T](
    units: Iterable[T],
    *,
    workers: int,
    name_prefix: str,
    run_unit: Callable[[T], None],
    describe: Callable[[T], str],
    log: Callable[[str], None],
    check_stop: Callable[[str], None] | None = None,
) -> int:
    """Run `run_unit` over `units`, up to `workers` at a time. Returns the
    number of worker threads actually used (1 ⇒ ran inline on this thread).

    `workers` is clamped to the unit count: a pool wider than the work spawns
    threads that immediately find an empty queue. One unit runs inline rather
    than paying a thread for a single call.
    """
    items = list(units)
    if not items:
        return 0
    n = max(1, min(workers, len(items)))
    if n == 1:
        for unit in items:
            if check_stop is not None:
                check_stop(f"{name_prefix} inline dispatch")
            try:
                run_unit(unit)
            except Exception as exc:
                log(f"  [{name_prefix}] worker error on {describe(unit)}: {exc}")
        return 1

    q: _queue_mod.Queue[T] = _queue_mod.Queue()
    for unit in items:
        q.put(unit)

    def _worker() -> None:
        while True:
            # Checked before CLAIMING work, not after: a stop requested
            # mid-pass should leave the queue undrained rather than start
            # another multi-minute model call.
            if check_stop is not None:
                check_stop(f"{name_prefix} worker dispatch")
            try:
                unit = q.get_nowait()
            except _queue_mod.Empty:
                return
            try:
                run_unit(unit)
            except Exception as exc:
                log(f"  [{name_prefix}] worker error on {describe(unit)}: {exc}")

    def _worker_in_context(ctx: contextvars.Context) -> Callable[[], None]:
        return lambda: ctx.run(_worker)

    threads = [threading.Thread(
        target=_worker_in_context(contextvars.copy_context()),
        name=f"{name_prefix}-w{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The caller's thread is the only one where a graceful stop can end the
    # process, so re-poll here after the join.
    if check_stop is not None:
        check_stop(f"{name_prefix} pool joined")
    return n
