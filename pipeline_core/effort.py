"""The reasoning level of the model call being made now.

Cross-cutting like :mod:`pipeline_core.phases` (any layer may import it).
`LLMController` calls :func:`call_effort` once per call, inside its server
slot, and passes the answer to every transport method that renders or plans
the call (`spec`, `count_input_tokens`, `stream`, `reasoning_payload`). No
transport's `stream` is reached except through the controller, so a new
caller cannot skip the decision.

The decision itself is `domain.effort_policy.effort_for`: the escalation
scope the caller holds (:func:`effort_scope`), else the first configured
phase on the call's phase path (:func:`pipeline_core.phases.current_scope`
and its parents), else the policy's default. The policy is read from the
environment once, at the first call, unless :func:`bind_policy` set it.

The escalation scope is a ContextVar, so a pooled worker continues it only
through :mod:`pipeline_core.threads` (`context_thread`, `ContextPool`), the
same way it continues the phase scope.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .domain.effort_policy import EffortPolicy, effort_for
from .domain.model_profile import EffortLevel
from .domain.phase_registry import known_ids, parent_of
from .phases import current_scope

__all__ = ["EffortLevel", "bind_policy", "call_effort", "current_escalation",
           "effort_scope", "phase_path", "policy"]

_policy: EffortPolicy | None = None
_lock = threading.Lock()
_escalation: ContextVar[EffortLevel | None] = ContextVar(
    "effort_escalation", default=None)


def policy() -> EffortPolicy:
    """The process's policy: bound, else read from the environment once."""
    global _policy
    with _lock:
        if _policy is None:
            _policy = EffortPolicy.from_env(os.environ, known_phases=known_ids())
        return _policy


def bind_policy(p: EffortPolicy | None) -> None:
    """Set the process's policy (None: read the environment again at the
    next call). For the composition root and tests."""
    global _policy
    with _lock:
        _policy = p


def phase_path() -> tuple[str, ...]:
    """The calling context's innermost open phase id and its parents,
    innermost first; empty outside every phase."""
    cur = current_scope()
    out: list[str] = []
    pid: str | None = cur[1] if cur is not None else None
    while pid:
        out.append(pid)
        pid = parent_of(pid)
    return tuple(out)


def current_escalation() -> EffortLevel | None:
    return _escalation.get()


@contextmanager
def effort_scope(level: EffortLevel | None) -> Iterator[None]:
    """Every call made in this context (and in workers it spawns through
    `pipeline_core.threads`) is sent at `level`, whatever the policy says.
    None holds no escalation: the policy decides."""
    token = _escalation.set(level)
    try:
        yield
    finally:
        _escalation.reset(token)


def call_effort() -> EffortLevel:
    """The level of the call being made now."""
    return effort_for(phase_path(), _escalation.get(), policy())
