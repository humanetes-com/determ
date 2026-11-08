"""How many model calls this process has on the server at once.

The server serves a fixed number of sequences (`docker_vllm.sh` starts vLLM
with `--max-num-seqs 4`). A request past that number is queued by the server
and receives no bytes until it is scheduled, while `VLLMTransport` reads with a
900 s timeout and the calls of task 0d and the fill run 4 to 15 minutes. So a
fifth concurrent call can time out in the queue and be re-sent by the ask
client's network retry. Every pool in the pipeline (the book's lanes, task 0c's
subset fan-out, the fill's unit pools, task 0d's lanes) sized itself on its own,
and nothing stopped two of them adding up past the server.

One slot set per process, taken by `LLMController` around one whole call (its
stream and its follow-up), so no pool, present or future, can put more calls on
the server than it serves: a new caller cannot skip it, because every
transport's `stream` is reached only through the controller.

Width: `LLM_INFLIGHT_MAX`, else `PIPELINE_PARALLELISM`, else 4 (the server's
sequence count). Read once, at the first call.

The time a call waits for a slot is returned to the controller, which emits it
(`llm.slot`, keyed on the prompt sha) so readers that time a call from
`llm.request` to `llm.response` can subtract it: a waiting call is not on the
server.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_DEFAULT_WIDTH = 4


def _width_from_env() -> int:
    for var in ("LLM_INFLIGHT_MAX", "PIPELINE_PARALLELISM"):
        raw = (os.getenv(var) or "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                continue
    return _DEFAULT_WIDTH


class InflightSlots:
    """A bounded number of concurrent model calls; `hold()` yields the
    milliseconds this call waited for its slot."""

    def __init__(self, width: int) -> None:
        self.width = max(1, int(width))
        self._sem = threading.BoundedSemaphore(self.width)

    @contextmanager
    def hold(self) -> Iterator[int]:
        t0 = time.monotonic()
        self._sem.acquire()
        try:
            yield int((time.monotonic() - t0) * 1000)
        finally:
            self._sem.release()


_lock = threading.Lock()
_slots: InflightSlots | None = None


def process_slots() -> InflightSlots:
    """The one slot set of this process (created at first use)."""
    global _slots
    if _slots is None:
        with _lock:
            if _slots is None:
                _slots = InflightSlots(_width_from_env())
    return _slots


def reset_process_slots(width: int | None = None) -> InflightSlots:
    """Replace the process slot set (tests; a width change needs no relaunch
    otherwise). `None` re-reads the environment."""
    global _slots
    with _lock:
        _slots = InflightSlots(_width_from_env() if width is None else width)
    return _slots
