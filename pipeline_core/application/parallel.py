"""Fan-out helper for the LLM batch path (C11).

`generate_in_parallel` fires one `ask` per query concurrently and returns the
responses index-aligned to the inputs. The `ask` and `guard` collaborators are
injected (the composition root binds the real client + truncation logger), so
this stays engine-independent and testable with fakes."""
from __future__ import annotations

from collections.abc import Callable

from pipeline_core.threads import context_thread


def generate_in_parallel(
    queries: list[str],
    *,
    ask: Callable[..., str],
    guard: Callable[[str, str], None],
    model: str | None = None,
    think: bool | None = None,
) -> list[str]:
    """One `ask()` per query, concurrently; responses index-aligned to `queries`.

    Used by task0c split-and-merge: the N subset prompts share a large file-
    context prefix and differ only in a small tail, so vLLM's prefix cache
    prefills the shared part once. A failed/empty call contributes "" rather than
    aborting the batch; len<=1 short-circuits to a single synchronous call."""
    if not queries:
        return []
    if len(queries) == 1:
        try:
            return [ask(queries[0], model=model, think=think)]
        except Exception:
            return [""]

    results: list[str] = [""] * len(queries)

    def _worker(i: int) -> None:
        try:
            results[i] = ask(queries[i], model=model, think=think) or ""
        except Exception as _e:  # one bad request must not sink the batch
            guard("task0c/candidate_error", f"request {i}: {type(_e).__name__}: {_e}")
            results[i] = ""

    threads = [context_thread(_worker, args=(i,), name=f"task0c-gen-{i}")
               for i in range(len(queries))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
