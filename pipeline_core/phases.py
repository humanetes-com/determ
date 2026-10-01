"""Announcing phases: the one way a pipeline phase says it started and ended.

Cross-cutting like :mod:`pipeline_core.log` (any layer may import it): it emits
`phase.enter` / `phase.exit` through the ambient `emit` and keeps the
`current:` line of `pipeline_state.md` in step, so the two records of "where
the run is" cannot disagree the way they did on run `34607-00cca24e`
(2026-09-30), when the strip, the bake and the behaviour book ran for 24
minutes under a state line that still said `task0c/nightly / starting`.

The shape callers use:

    with phase_scope("bake", module=name) as ph:
        ph.step("bake.preserve")
        ...
        ph.step("bake.api")
        ...

and, inside a function the scope's owner calls but does not hand a handle to,
the ambient form `step("bake.book", cluster=cluster.name)`, which finds the
innermost open scope whose phase is the step's parent. Steps are sequential:
entering one closes the previous, and leaving the scope closes the last.

Only ids the registry knows are announced as themselves. An unknown id is
emitted anyway, flagged `unknown`, and logged loudly: observability must not
crash a run, and `tests/test_phase_registry.py` scans every literal id at every
call site so an unknown one cannot ship.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from types import TracebackType
from typing import Any

from .domain.phase_registry import PHASE_ENTER, PHASE_EXIT, parent_of
from .domain.phase_registry import phase as _phase
from .log import emit, log

__all__ = ["PhaseScope", "phase_scope", "phase_skipped", "step", "bind_current"]

_current_sink: Callable[[str], None] | None = None
_open: list[PhaseScope] = []
_lock = threading.Lock()


def bind_current(sink: Callable[[str], None] | None) -> None:
    """Point the `current:` writer at the live workspace's state file. Called
    once per run by the orchestrator, which owns `project_dir`; `None`
    unbinds (tests)."""
    global _current_sink
    _current_sink = sink


def _set_current(label: str) -> None:
    sink = _current_sink
    if sink is None:
        return
    with suppress(Exception):  # observability must never crash the pipeline
        sink(label)


def _label(phase_id: str, cluster: str | None) -> str:
    p = _phase(phase_id)
    where = f"/{cluster}" if cluster else ""
    return f"{phase_id}{where} / {p.title if p else 'running'}"


def _fields(phase_id: str, cluster: str | None, module: str | None,
            attempt: int | None, detail: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"phase": phase_id, "cluster": cluster, "module": module,
                           "attempt": attempt}
    data: dict[str, Any] = {}
    if detail:
        data["detail"] = detail
    if _phase(phase_id) is None:
        data["unknown"] = True
        log(f"  [phases] HEALTH ‼ unknown phase id {phase_id!r} announced — "
            "not in domain.phase_registry")
    if data:
        out["data"] = data
    return out


class PhaseScope:
    """One open phase and, at most, one open step inside it."""

    def __init__(self, phase_id: str, *, cluster: str | None = None,
                 module: str | None = None, attempt: int | None = None,
                 detail: str | None = None) -> None:
        self.phase_id = phase_id
        self.cluster = cluster
        self.module = module
        self.attempt = attempt
        self.detail = detail
        self._t0 = 0.0
        self._step: tuple[str, str | None, float] | None = None
        self._closed = False

    # -- open / close ------------------------------------------------------
    def open(self) -> PhaseScope:
        """Enter the phase. The context manager calls this; straight-line code
        too long to indent under a `with` (the orchestrator's setup) calls it
        and `close` directly."""
        self._t0 = time.monotonic()
        with _lock:
            _open.append(self)
        emit(PHASE_ENTER, **_fields(self.phase_id, self.cluster, self.module,
                                    self.attempt, self.detail))
        _set_current(_label(self.phase_id, self.cluster))
        return self

    def close(self, outcome: str = "ok") -> None:
        self._close_step(outcome)
        self._exit(outcome)

    def __enter__(self) -> PhaseScope:
        return self.open()

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        if et is None:
            outcome = "ok"
        elif issubclass(et, SystemExit):
            # `sys.exit(0)` is how a completed run leaves the lock phase.
            outcome = "ok" if getattr(ev, "code", None) in (0, None) else "error"
        elif issubclass(et, Exception):
            outcome = "error"
        else:
            outcome = "aborted"
        self.close(outcome)

    # -- steps -------------------------------------------------------------
    def step(self, child_id: str, *, cluster: str | None = None,
             detail: str | None = None) -> None:
        """Close the open step (ok) and enter `child_id`."""
        self._close_step("ok")
        if parent_of(child_id) != self.phase_id:
            log(f"  [phases] HEALTH ‼ step {child_id!r} is not a child of "
                f"{self.phase_id!r}")
        cl = cluster if cluster is not None else self.cluster
        self._step = (child_id, cl, time.monotonic())
        emit(PHASE_ENTER, **_fields(child_id, cl, self.module, self.attempt, detail))
        _set_current(_label(child_id, cl))

    def skip(self, reason: str) -> None:
        """The phase's own checkpoint says it is already done: close it as
        skipped. The context manager's exit then has nothing left to say."""
        self._close_step("skipped")
        self._exit("skipped", detail=reason)

    # -- internals ---------------------------------------------------------
    def _close_step(self, outcome: str) -> None:
        if self._step is None:
            return
        child_id, cl, t0 = self._step
        self._step = None
        emit(PHASE_EXIT, **_fields(child_id, cl, self.module, self.attempt, None),
             outcome=outcome, duration_ms=int((time.monotonic() - t0) * 1000))

    def _exit(self, outcome: str, *, detail: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        with _lock:
            if self in _open:
                _open.remove(self)
        emit(PHASE_EXIT, **_fields(self.phase_id, self.cluster, self.module,
                                   self.attempt, detail),
             outcome=outcome, duration_ms=int((time.monotonic() - self._t0) * 1000))


def phase_scope(phase_id: str, *, cluster: str | None = None,
                module: str | None = None, attempt: int | None = None,
                detail: str | None = None) -> PhaseScope:
    return PhaseScope(phase_id, cluster=cluster, module=module, attempt=attempt,
                      detail=detail)


def phase_skipped(phase_id: str, reason: str, *, cluster: str | None = None,
                  module: str | None = None) -> None:
    """A phase that did not run because its checkpoint says it is done."""
    emit(PHASE_EXIT, **_fields(phase_id, cluster, module, None, reason),
         outcome="skipped", duration_ms=0)


def step(child_id: str, *, cluster: str | None = None,
         detail: str | None = None) -> None:
    """Enter a step of the innermost open scope whose phase is its parent."""
    parent = parent_of(child_id)
    with _lock:
        owner = next((s for s in reversed(_open) if s.phase_id == parent), None)
    if owner is None:
        log(f"  [phases] HEALTH ‼ step {child_id!r} announced with no open "
            f"{parent!r} scope")
        emit(PHASE_ENTER, **_fields(child_id, cluster, None, None, detail))
        return
    owner.step(child_id, cluster=cluster, detail=detail)
