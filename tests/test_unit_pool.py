"""`run_units_concurrently` — the dispatch the fill routes share.

The file-first route ran its units in a serial `for` loop on the main thread
until 2026-08-09. Measured on run `491052-1af6b4f4` by pairing
`llm.request`/`llm.response` in `logs/events.jsonl` into 5-second slots: 47 % of
slots had a MainThread call in flight, and across those slots mean concurrency
was **1.03** — the `per-fn-*` and `impl-fill-*` pools sat idle behind it —
against 2.85 while MainThread was free. 20 of the run's 68 MainThread calls came
from that loop at a mean of 193 s each.

Concurrency is proven with a BARRIER rather than with timing: a barrier that
releases only when N workers have arrived cannot be satisfied by a fast serial
loop, so the test cannot pass by accident on a slow machine or fail by flaking
on a loaded one.
"""
from __future__ import annotations

import threading

import pytest

from pipeline_core.application.reconstruct.unit_pool import run_units_concurrently


def _noop_log(_m: str) -> None:
    return None


def test_units_run_genuinely_concurrently() -> None:
    """Four units, four workers, and a barrier all four must reach. A serial
    loop deadlocks here and the test times out rather than passing."""
    barrier = threading.Barrier(4, timeout=10)
    threads: list[str] = []
    lock = threading.Lock()

    def _run(_u: int) -> None:
        barrier.wait()
        with lock:
            threads.append(threading.current_thread().name)

    n = run_units_concurrently(
        range(4), workers=4, name_prefix="ff", run_unit=_run,
        describe=str, log=_noop_log)

    assert n == 4
    assert len(set(threads)) == 4, f"units shared threads: {threads}"
    assert all(t.startswith("ff-w") for t in threads), threads


def test_a_unit_is_owned_by_one_worker_start_to_finish() -> None:
    """Sibling impls on one type are co-designed, so a unit must never be split
    across workers. Each unit records the thread at entry and at exit."""
    seen: list[tuple[int, str, str]] = []
    lock = threading.Lock()

    def _run(u: int) -> None:
        entry = threading.current_thread().name
        threading.Event().wait(0.01)
        with lock:
            seen.append((u, entry, threading.current_thread().name))

    run_units_concurrently(range(12), workers=4, name_prefix="ff",
                           run_unit=_run, describe=str, log=_noop_log)

    assert len(seen) == 12
    assert all(entry == exit_ for _u, entry, exit_ in seen)


def test_every_unit_runs_exactly_once() -> None:
    counts: dict[int, int] = {}
    lock = threading.Lock()

    def _run(u: int) -> None:
        with lock:
            counts[u] = counts.get(u, 0) + 1

    run_units_concurrently(range(50), workers=4, name_prefix="ff",
                           run_unit=_run, describe=str, log=_noop_log)

    assert counts == dict.fromkeys(range(50), 1)


def test_one_units_failure_does_not_take_its_neighbours() -> None:
    """`_fill_file_first` returns rather than raises on an LLM error, but a
    splice or a parse can still throw. One file's exception must not abandon
    the other files' work — before the pool, the serial loop had no containment
    at all."""
    done: list[int] = []
    logged: list[str] = []
    lock = threading.Lock()

    def _run(u: int) -> None:
        if u == 3:
            raise RuntimeError("splice refused")
        with lock:
            done.append(u)

    run_units_concurrently(range(8), workers=4, name_prefix="ff",
                           run_unit=_run, describe=lambda u: f"src/f{u}.rs",
                           log=logged.append)

    assert sorted(done) == [0, 1, 2, 4, 5, 6, 7]
    assert any("src/f3.rs" in m and "splice refused" in m for m in logged), logged


def test_a_single_unit_runs_inline_without_a_thread() -> None:
    """One unit is one model call; paying a thread for it buys nothing."""
    where: list[str] = []
    n = run_units_concurrently(
        ["only"], workers=4, name_prefix="ff",
        run_unit=lambda _u: where.append(threading.current_thread().name),
        describe=str, log=_noop_log)

    assert n == 1
    assert where == [threading.current_thread().name]


def test_no_units_spawns_nothing() -> None:
    assert run_units_concurrently(
        [], workers=4, name_prefix="ff", run_unit=lambda _u: None,
        describe=str, log=_noop_log) == 0


def test_pool_is_never_wider_than_the_work() -> None:
    """A pool wider than the queue spawns threads that immediately find it
    empty. Clamping keeps the `impl-fill-w3`-style names in the logs meaningful
    as evidence of real concurrency."""
    names: set[str] = set()
    lock = threading.Lock()

    def _run(_u: int) -> None:
        with lock:
            names.add(threading.current_thread().name)

    n = run_units_concurrently(range(2), workers=8, name_prefix="ff",
                               run_unit=_run, describe=str, log=_noop_log)

    assert n == 2
    assert len(names) <= 2


def test_a_graceful_stop_drains_rather_than_aborts() -> None:
    """`check_stop` is polled before a unit is CLAIMED, so a stop requested
    mid-pass does not start another multi-minute model call — and it is
    re-raised on the calling thread after the join, because `sys.exit()` in a
    worker kills that worker alone."""
    started: list[int] = []
    lock = threading.Lock()

    class _Stop(Exception):
        pass

    def _check(_where: str) -> None:
        with lock:
            if len(started) >= 2:
                raise _Stop(_where)

    def _run(u: int) -> None:
        with lock:
            started.append(u)

    with pytest.raises(_Stop):
        run_units_concurrently(range(20), workers=1, name_prefix="ff",
                               run_unit=_run, describe=str, log=_noop_log,
                               check_stop=_check)

    assert len(started) == 2, "the stop did not halt further claims"


def test_the_stop_is_re_raised_on_the_calling_thread_after_a_join() -> None:
    """The pool path, not the inline one: workers swallow nothing, and the
    caller still learns the run was stopped."""
    class _Stop(Exception):
        pass

    calls: list[str] = []
    lock = threading.Lock()

    def _check(where: str) -> None:
        with lock:
            calls.append(where)
        if where.endswith("pool joined"):
            raise _Stop(where)

    with pytest.raises(_Stop):
        run_units_concurrently(range(6), workers=3, name_prefix="ff",
                               run_unit=lambda _u: None, describe=str,
                               log=_noop_log, check_stop=_check)

    assert any(c.endswith("pool joined") for c in calls)
