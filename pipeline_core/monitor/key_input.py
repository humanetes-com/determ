"""Threaded terminal key reader — decodes keypresses off the render loop.

WHY THIS EXISTS. `monitor.py`'s render loop used to read stdin inline: a
keypress sat unread in the terminal's input buffer until the *next*
render+data-collection pass finished (log tail reads, nvidia-smi, ...) — cheap
most of the time, but on a slow frame it read as "press it again". `KeyReader`
runs the decode on its own thread, blocked on stdin independently of the
render loop, and hands logical keys to the render loop through a queue: a key
is picked off stdin the instant it arrives, and a burst of presses queues up
rather than getting dropped between `select()` polls.
"""
from __future__ import annotations

import os
import queue
import select
import sys
import threading


class KeyReader:
    """Owns the reader thread and the queue the render loop drains.

    `start()`/`stop()` bracket one `main()` invocation; `get`/`get_nowait`
    mirror `queue.Queue` so the render loop's drain-the-burst logic reads the
    same either way.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get(self, timeout: float) -> str:
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> str:
        return self._queue.get_nowait()

    def _reader_loop(self) -> None:
        """Thread body: decode logical keypresses and push them to the queue.

        Uses os.read() throughout to bypass Python's BufferedReader — without
        this, select.select() misses continuation bytes the buffer has already
        consumed. The 0.2s poll timeout is only so the thread notices `stop`
        promptly on shutdown; it does not add key latency since select()
        returns immediately once a byte is actually available.
        """
        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            try:
                ch = os.read(fd, 1).decode("latin-1")
            except OSError:
                return
            if ch != "\033":
                self._queue.put(ch)
                continue
            # Escape sequence — read more with short timeout
            if not select.select([fd], [], [], 0.05)[0]:
                self._queue.put("ESC")
                continue
            ch2 = os.read(fd, 1).decode("latin-1")
            if ch2 != "[":
                self._queue.put("ESC")
                continue
            if not select.select([fd], [], [], 0.05)[0]:
                self._queue.put("ESC")
                continue
            ch3 = os.read(fd, 1).decode("latin-1")
            if ch3 == "A":
                self._queue.put("UP")
            elif ch3 == "B":
                self._queue.put("DOWN")
            elif ch3 == "5":
                os.read(fd, 1)   # consume trailing ~
                self._queue.put("PGUP")
            elif ch3 == "6":
                os.read(fd, 1)
                self._queue.put("PGDN")
            else:
                self._queue.put("ESC")
