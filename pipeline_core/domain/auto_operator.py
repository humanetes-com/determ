"""Auto-operator state machine — the logic behind the monitor's ``[a]`` mode.

When enabled, the monitor watches the active cluster's TDD stall count and, when
it reaches a trigger, launches one full operator pass (``operator_run.sh``) to
break the plateau. The rules that make that safe live here as a pure, testable
state machine (no subprocess, no curses): fire ONCE per stall episode, never
overlap a pass that is still running, and re-arm only after the stall clears
(e.g. the pass restarts the pipeline and the floor resets). The monitor supplies
the current stall, a ``launch`` callback, and an opaque process handle whose only
required method is ``poll()`` (``None`` while running).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def active_stall(state: Any) -> int:
    """The active cluster's TDD stall from a monitor phase-C state dict; 0 when
    no Phase C is active or the value is missing/unparseable."""
    if not isinstance(state, dict) or not state.get("active"):
        return 0
    cs = (state.get("tdd_by_cluster") or {}).get(state.get("cluster")) or {}
    raw = cs.get("stall")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


@dataclass
class AutoOperator:
    """Stall-triggered auto-operator state. `proc` is an opaque handle (anything
    exposing ``poll() -> None|exitcode``); `armed` gates one firing per episode."""

    trigger: int
    enabled: bool = False
    armed: bool = True
    proc: Any = None

    @property
    def running(self) -> bool:
        return self.proc is not None

    def toggle(self) -> None:
        """Flip the mode on/off; always (re-)arm so turning it on can fire at
        the very next stall reading."""
        self.enabled = not self.enabled
        self.armed = True

    def tick(self, stall: int, launch: Callable[[], Any]) -> None:
        """One monitor frame: reap a finished pass, then fire `launch()` iff the
        mode is on, we are armed, nothing is running, and the stall has reached
        the trigger. Below the trigger the episode is over → re-arm."""
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
        if not self.enabled:
            return
        if stall < self.trigger:
            self.armed = True
            return
        if self.armed and self.proc is None:
            self.proc = launch()
            self.armed = False

    def label(self) -> tuple[str, str]:
        """(color-name, text) for the monitor's bottom-bar ``[a]uto`` segment."""
        if self.running:
            return ("green", "[a]uto ⚙running")
        if self.enabled:
            return ("cyan", f"[a]uto ON ≥{self.trigger}")
        return ("dim", "[a]uto")
