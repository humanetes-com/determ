"""Why the monitor exited — one line per exit, on disk.

The monitor used to vanish with nothing on screen (its exit path clears the
terminal) and nothing on disk, so a quit key decoded out of a terminal report,
a signal and a crash all looked the same (2026-09-30). Every exit now appends
one line: the quit key with the raw input it was decoded from, the signal, or
the traceback. A "q" that nobody typed shows up as the report bytes around it.
"""
from __future__ import annotations

import contextlib
import os
import signal
import time
import traceback


class Quit(BaseException):
    """Ends the monitor's main loop. A BaseException so a SIGTERM raised
    mid-frame is not taken for a render error by the loop's `except Exception`."""


def key_quit(key: str, recent_input: bytes) -> Quit:
    return Quit(f"quit: {key} key; input in the last 5s: {recent_input!r}")


def on_sigterm(signum: int, _frame: object) -> None:
    raise Quit(f"quit: signal {signal.Signals(signum).name}")


def describe(exc: BaseException) -> str:
    if isinstance(exc, Quit):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return "quit: SIGINT (Ctrl-C)"
    return "crash: " + "".join(traceback.format_exception(exc)).replace("\n", "\n    ")


def record(path: str, reason: str) -> None:
    with contextlib.suppress(OSError), open(path, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {reason}\n")
