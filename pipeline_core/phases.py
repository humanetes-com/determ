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

Every scope and step gets a `scope` id, announced on its enter and exit with
the `parent_scope` it was opened under, and becomes the current scope of the
context that opened it (:func:`current_scope`). The event emitter stamps that
scope on every other event, so an `llm.request` or a cargo spawn names the
phase that paid for it, including from a pooled worker thread (see
:mod:`pipeline_core.threads`): `domain.phase_cost.PhaseCostFold` folds the
stream into per-phase cost.

Only ids the registry knows are announced as themselves. An unknown id is
emitted anyway, flagged `unknown`, and logged loudly: observability must not
crash a run, and `tests/test_phase_registry.py` scans every literal id at every
call site so an unknown one cannot ship.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from types import TracebackType
from typing import Any

from .domain.phase_registry import PHASE_ENTER, PHASE_EXIT, PHASE_PROGRESS, parent_of
from .domain.phase_registry import phase as _phase
from .log import emit, log

__all__ = ["PhaseScope", "phase_scope", "phase_skipped", "step", "note_work",
           "bind_current", "current_scope"]

_current_sink: Callable[[str], None] | None = None
_open: list[PhaseScope] = []
_lock = threading.Lock()
# (scope id, phase id) of the innermost scope or step open in THIS context.
_scope: ContextVar[tuple[str, str] | None] = ContextVar("phase_scope", default=None)
_scope_ids = itertools.count(1)


def current_scope() -> tuple[str, str] | None:
    """`(scope id, phase id)` of the innermost phase or step open in the
    calling context, or None outside every phase."""
    return _scope.get()


def _new_scope_id() -> str:
    return str(next(_scope_ids))


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
            attempt: int | None, detail: str | None, *,
            scope: str | None = None, parent_scope: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"phase": phase_id, "cluster": cluster, "module": module,
                           "attempt": attempt, "scope": scope,
                           "parent_scope": parent_scope}
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
        self._step_scope: str | None = None
        self._closed = False
        self.scope_id = ""
        self._outer: tuple[str, str] | None = None

    # -- open / close ------------------------------------------------------
    def open(self) -> PhaseScope:
        """Enter the phase. The context manager calls this; straight-line code
        too long to indent under a `with` (the orchestrator's setup) calls it
        and `close` directly."""
        self._t0 = time.monotonic()
        self.scope_id = _new_scope_id()
        self._outer = _scope.get()
        with _lock:
            _open.append(self)
        emit(PHASE_ENTER, **_fields(
            self.phase_id, self.cluster, self.module, self.attempt, self.detail,
            scope=self.scope_id,
            parent_scope=self._outer[0] if self._outer else None))
        _scope.set((self.scope_id, self.phase_id))
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
        self._step_scope = _new_scope_id()
        emit(PHASE_ENTER, **_fields(child_id, cl, self.module, self.attempt, detail,
                                    scope=self._step_scope,
                                    parent_scope=self.scope_id))
        _scope.set((self._step_scope, child_id))
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
        sid, self._step, self._step_scope = self._step_scope, None, None
        emit(PHASE_EXIT, **_fields(child_id, cl, self.module, self.attempt, None,
                                   scope=sid, parent_scope=self.scope_id),
             outcome=outcome, duration_ms=int((time.monotonic() - t0) * 1000))
        _scope.set((self.scope_id, self.phase_id))

    def _exit(self, outcome: str, *, detail: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        with _lock:
            if self in _open:
                _open.remove(self)
        emit(PHASE_EXIT, **_fields(
            self.phase_id, self.cluster, self.module, self.attempt, detail,
            scope=self.scope_id,
            parent_scope=self._outer[0] if self._outer else None),
             outcome=outcome, duration_ms=int((time.monotonic() - self._t0) * 1000))
        _scope.set(self._outer)


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


def note_work(*, done: int, total: int, calls: int, lanes: int, wave: str) -> None:
    """How far the open book step is through the plan it already counted.

    The book stays one step for the whole build, so enter/exit cannot move a
    bar. This leaves the step open and reports the count: `chunks` while the
    draft-and-judge wave runs, `siblings` once that wave's visits are known.
    No open book step means the build is not on the strip (a unit test, a
    caller that has not entered the step): say nothing rather than invent a
    phase. `wave` is only those two names; anything else is dropped."""
    if wave not in ("chunks", "siblings") or total <= 0:
        return
    with _lock:
        owner = next((s for s in reversed(_open)
                      if s._step is not None and s._step[0].endswith(".book")), None)
        step_at = owner._step if owner is not None else None
    if owner is None or step_at is None:
        return
    child_id, cl, _t0 = step_at
    emit(PHASE_PROGRESS, phase=child_id,
         cluster=cl, module=owner.module, attempt=owner.attempt,
         data={"wave": wave, "done": done, "total": total,
               "calls": calls, "lanes": lanes})


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
