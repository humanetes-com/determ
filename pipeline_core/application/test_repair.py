"""One repair loop for a test a writer proposed that did not build.

Every writer that adds a test to the oracle asks a model for a candidate,
runs it on the original crate, and keeps it only when it builds and passes.
A candidate that does not build carries the compiler's diagnostics, and
the cheapest answer to it is the same candidate with those diagnostics
handed back. The census of 2026-10-20 (`scripts/test_writer_census.py`)
counted that per writer over the rotated logs: task 0c's blocks needed it
on 431 of 981 and its fix ask recovered 266 (62%); task 0d's probes, which
had no repair, dropped 19 of 72 (run `202215-c5c6347b`) and 25 of 48
(run `486447-5207101c`) for not building.

The loop is the writer-independent half: run, read the diagnostics off the
result, ask for a corrected candidate in the writer's own shape, run again,
for at most `attempts` asks. What a candidate is, how it runs, what counts
as a diagnostic and how the writer asks are the caller's.
"""
from __future__ import annotations

from collections.abc import Callable


def repair_until_built[C, R](
    candidate: C,
    *,
    run: Callable[[C], R],
    diagnostics: Callable[[R], str | None],
    ask: Callable[[C, str], C | None],
    attempts: int,
) -> tuple[C, R, int]:
    """(the last candidate run, its result, repair asks spent).

    Stops when a result carries no diagnostics (it built, or failed for a
    reason no compiler named), when the writer's ask returns no candidate,
    or after `attempts` asks."""
    result = run(candidate)
    asks = 0
    while asks < attempts:
        diag = diagnostics(result)
        if diag is None:
            break
        fixed = ask(candidate, diag)
        asks += 1
        if fixed is None:
            break
        candidate = fixed
        result = run(candidate)
    return candidate, result, asks
