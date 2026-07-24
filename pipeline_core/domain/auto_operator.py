"""Auto-operator state machine — the logic behind stall-triggered auto mode.

When enabled, the pipeline watches the active cluster's TDD stall count and, when
it reaches a trigger, launches one full operator pass (``operator_run.sh``) to
break the plateau. The rules that make that safe live here as a pure, testable
state machine (no subprocess, no file I/O): fire ONCE per stall episode, never
overlap a pass that is still running, and re-arm only after the stall clears
(e.g. the pass restarts the pipeline and the floor resets). The caller supplies
whether the mode is currently enabled (sentinel/`.env`-driven — see
`pipeline_core.adapters.auto_operator`), the current stall, a ``launch``
callback, and an opaque process handle whose only required method is
``poll()`` (``None`` while running).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class AutoOperator:
    """Stall-triggered auto-operator state. `proc` is an opaque handle (anything
    exposing ``poll() -> None|exitcode``); `armed` gates one firing per episode."""

    trigger: int
    armed: bool = True
    proc: Any = None

    @property
    def running(self) -> bool:
        return self.proc is not None

    def tick(self, enabled: bool, stall: int, launch: Callable[[], Any]) -> bool:
        """One frame: reap a finished pass, then fire `launch()` iff `enabled`
        is true, we are armed, nothing is running, and the stall has reached
        the trigger. Below the trigger (or disabled) the episode is over —
        re-arm so the next stall reading can fire again. Returns whether this
        frame fired: a caller cannot infer it from `running` alone, because a
        single frame can BOTH reap the previous pass and launch a new one
        (re-armed by a dip below the trigger while the old pass was still
        running), leaving `running` true before and after."""
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
        if not enabled or stall < self.trigger:
            self.armed = True
            return False
        if self.armed and self.proc is None:
            self.proc = launch()
            self.armed = False
            return True
        return False
