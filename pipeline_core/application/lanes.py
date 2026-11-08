"""Run independent model calls side by side, results in item order.

The one lane helper of the application layer. It decides only how many items
are worked at once on this caller's behalf; how many calls the SERVER holds at
once is the process-wide slot set the LLM controller takes around every call
(`pipeline_core/llm/slots.py`), so two callers' lanes added together can never
put more calls on the server than it serves.

Contract, relied on by every caller:

- results come back in item order, whatever order the items finish in, so a
  caller whose later steps read the results in order (captures numbered by
  position, a memo filled in order) is byte-for-byte the serial run at any
  width, and `lanes=1` IS the serial run (the A/B and the negative control);
- `on_done` runs on the calling thread as each result arrives;
- the worker threads continue the caller's context (phase scope, run id) via
  `ContextPool`, and are named `<name>-<n>` so each call's stream file and
  event `thread` are distinct;
- an exception in one item is raised to the caller after every submitted
  item has finished, exactly as the serial loop would have raised it at that
  item (later items' results are then discarded).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import as_completed

from pipeline_core.threads import ContextPool


def on_lanes[T, R](fn: Callable[[T], R], items: Sequence[T], lanes: int,
                   on_done: Callable[[R], None] | None = None,
                   *, name: str = "lane") -> list[R]:
    """`fn` over `items`, results in item order, on up to `lanes` threads."""
    if not items:
        return []
    if lanes > 1 and len(items) > 1:
        width = min(lanes, len(items))
        with ContextPool(max_workers=width, thread_name_prefix=name) as pool:
            pending = {pool.submit(fn, item): i for i, item in enumerate(items)}
            done: dict[int, R] = {}
            failed: dict[int, BaseException] = {}
            for fut in as_completed(pending):
                i = pending[fut]
                exc = fut.exception()
                if exc is not None:
                    failed[i] = exc
                    continue
                done[i] = fut.result()
                if on_done is not None:
                    on_done(done[i])
        if failed:
            raise failed[min(failed)]
        return [done[i] for i in range(len(items))]
    out: list[R] = []
    for item in items:
        result = fn(item)
        out.append(result)
        if on_done is not None:
            on_done(result)
    return out
