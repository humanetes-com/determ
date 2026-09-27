"""Why one test's own process aborted, read once from what that process
printed — the named cause the one-test-per-process outcome was missing.

Isolation (`test_isolation`) gives every aborted test its own process and
its own failure block: `ABORT_MARK`, then everything the process printed
after its section opened, then `(exit status N)`. What the runtime printed
there already says how the process died, and nothing read it: the fault
class was read off the detector's kind string alone (`crash_site.
fault_class`), so a stack overflow the debugger reported as `SIGSEGV` was
classed as invalid memory — the lifetime prescription, no cycle read — and
a panic in a destructor (a `SIGABRT`) was classed the same way. Run
`38934-2999cc94` (2026-09-26): 13 of 14 failing tests died with the
runtime's own `has overflowed its stack` line in exactly this block.

The cause is the runtime's own announcement, never a guess from a signal
number: the signal is kept as a fallback reading only (`SIGNAL`).

- ``STACK_EXHAUSTION`` — the runtime's stack-overflow announcement;
- ``PANIC_IN_PANIC`` — the runtime aborted a panic it could not unwind
  (a panic while panicking, a panic in a destructor during cleanup, a
  non-unwinding panic);
- ``SIGNAL`` — no announcement; the process ended on a signal (the exit
  status the isolation runner recorded is negative);
- ``UNKNOWN`` — none of the above.

Which body panicked (a stub, crate code) is not a cause; the stub-hold and
panic-site readings already answer that from the same block.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["AbortCause", "AbortReading", "read_abort"]


class AbortCause(StrEnum):
    STACK_EXHAUSTION = "stack_exhaustion"
    PANIC_IN_PANIC = "panic_in_panic"
    SIGNAL = "signal"
    UNKNOWN = "unknown"


# The Rust runtime's own lines, as measured on rustc 1.95: std's
# stack-overflow handler (run `38934-2999cc94`'s isolated blocks) and
# `core::panicking::panic_in_cleanup` (run `2585062-494e630e`, the lines
# `test_report` already keys the stub-hold reading on).
_EXHAUSTION = ("has overflowed its stack", "fatal runtime error: stack overflow")
_PANIC_IN_PANIC = (
    "panic in a destructor during cleanup",
    "thread caused non-unwinding panic",
)
_EXIT = "(exit status "


@dataclass(frozen=True, slots=True)
class AbortReading:
    cause: AbortCause
    #: The signal number the process ended on, when the capture says.
    signal: int | None = None

    def label(self) -> str:
        if self.cause is AbortCause.SIGNAL and self.signal is not None:
            return f"signal {self.signal}"
        return self.cause.value


def _exit_signal(text: str) -> int | None:
    i = text.rfind(_EXIT)
    if i < 0:
        return None
    raw = text[i + len(_EXIT):].split(")", 1)[0].strip()
    try:
        rc = int(raw)
    except ValueError:
        return None
    return -rc if rc < 0 else None


def read_abort(block: str) -> AbortReading:
    """The cause an aborted test's own block states (``block`` is its
    failure-block text: what its process printed, then the exit status)."""
    sig = _exit_signal(block)
    if any(m in block for m in _EXHAUSTION):
        return AbortReading(AbortCause.STACK_EXHAUSTION, sig)
    if any(m in block for m in _PANIC_IN_PANIC):
        return AbortReading(AbortCause.PANIC_IN_PANIC, sig)
    if sig is not None:
        return AbortReading(AbortCause.SIGNAL, sig)
    return AbortReading(AbortCause.UNKNOWN)
