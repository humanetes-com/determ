"""Threaded terminal key reader — decodes keypresses off the render loop.

WHY THIS EXISTS. `monitor.py`'s render loop used to read stdin inline: a
keypress sat unread in the terminal's input buffer until the *next*
render+data-collection pass finished (log tail reads, nvidia-smi, ...) — cheap
most of the time, but on a slow frame it read as "press it again". `KeyReader`
runs the decode on its own thread, blocked on stdin independently of the
render loop, and hands logical keys to the render loop through a queue: a key
is picked off stdin the instant it arrives, and a burst of presses queues up
rather than getting dropped between `select()` polls.

WHAT A KEY IS. Stdin carries more than keypresses: the terminal also reports
mouse events, focus changes, pasted text and replies to queries some other
program sent (a colour query's OSC reply, a DCS status string). Every one of
the monitor's commands is a single byte, so any byte of those reports that
reaches the queue is a command nobody typed: a "q" quits, a digit or "d"/"e"/
"l"/"h"/"f" switches the view, "a" toggles the auto-operator, "c" launches
`continue.sh`. Measured 2026-09-30 through a pty against the CSI-only decoder
this replaced: a legacy mouse report at column 81 (`ESC [ M SP q 2`) leaked
"q" and "2", a wheel event leaked "a" and "1", an OSC 11 colour reply leaked
eleven digits, a DCS reply and a bracketed paste leaked their payload, and a
sequence whose ESC arrived 80 ms before its tail leaked whole. `KeyDecoder`
is the one model of that stream: it parses every ECMA-48 / xterm input form to
its end and emits a key only for the forms that ARE a keypress. A report it
does not name is dropped, never turned into "ESC" — a focus change is not the
Escape key, and "ESC" cancels a pending confirmation.
"""
from __future__ import annotations

import collections
import os
import queue
import re
import select
import sys
import threading
import time

# A lone ESC is the Escape key only if nothing follows it within this window;
# a terminal writes a sequence in one burst, so its tail arrives well inside it.
ESC_WAIT = 0.1
# Once a sequence has started, its end is waited for this long. A sequence cut
# short (a remote link that stalled mid-report) is dropped, not decoded as keys.
SEQ_WAIT = 0.5
# A CSI longer than this has no final byte coming; drop it rather than swallow
# the keys typed after it.
_CSI_MAX = 64

_GROUND, _ESC, _SS3, _CSI, _STRING, _STRING_ESC, _MOUSE, _PASTE = range(8)

# The string introducers after ESC: DCS P, SOS X, OSC ], PM ^, APC _ — each
# runs to ST (ESC \) or BEL and carries a payload that is never keys.
_STRING_INTRO = frozenset("PX]^_")
# kitty keyboard protocol: CSI <codepoint> [; 1] u is an unmodified keypress.
_CSI_U_KEY = re.compile(r"(\d+)(?:;1)?u")
_PASTE_END = "\x1b[201~"


def _csi_key(seq: str) -> str | None:
    """The key a complete CSI (parameters + final byte) stands for, or None.

    A private-parameter sequence (`<` `=` `>` `?` first) is always a report —
    SGR mouse, a DA/DECRQM reply — never a key.
    """
    if seq[:1] in ("<", "=", ">", "?"):
        return None
    final, params = seq[-1], seq[:-1]
    if final in "AB":
        return "UP" if final == "A" else "DOWN"
    if final == "~":
        return {"5": "PGUP", "6": "PGDN"}.get(params.split(";")[0])
    m = _CSI_U_KEY.fullmatch(seq)
    if m:
        code = int(m.group(1))
        if code == 27:
            return "ESC"
        if 0x20 <= code < 0x7f:
            return chr(code)
    return None


class KeyDecoder:
    """Byte stream in, logical keys out: `feed` bytes, `timeout` when idle.

    Pure (no I/O, no clock): the caller owns the waiting. `wait()` says how
    long the caller may block before `timeout()` must be called — None when
    no sequence is open, so the caller may block indefinitely.
    """

    def __init__(self) -> None:
        self._state = _GROUND
        self._seq = ""
        self._left = 0

    def wait(self) -> float | None:
        if self._state == _GROUND:
            return None
        return ESC_WAIT if self._state == _ESC else SEQ_WAIT

    def timeout(self) -> list[str]:
        """Nothing arrived within `wait()`: a lone ESC is the Escape key, any
        sequence still open is dropped."""
        lone_esc = self._state == _ESC
        self._state, self._seq = _GROUND, ""
        return ["ESC"] if lone_esc else []

    def feed(self, data: bytes) -> list[str]:
        keys: list[str] = []
        for ch in data.decode("latin-1"):
            key = self._step(ch)
            if key is not None:
                keys.append(key)
        return keys

    def _step(self, ch: str) -> str | None:
        st = self._state
        if st == _GROUND:
            if ch == "\x1b":
                self._state = _ESC
                return None
            return ch
        if st == _ESC:
            if ch == "[":
                self._state, self._seq = _CSI, ""
            elif ch == "O":
                self._state = _SS3
            elif ch in _STRING_INTRO:
                self._state = _STRING
            elif ch == "\x1b":
                return "ESC"   # Escape pressed, then a new sequence starts
            else:
                self._state = _GROUND   # Alt+key: not one of the monitor's keys
            return None
        if st == _SS3:
            self._state = _GROUND
            return {"A": "UP", "B": "DOWN"}.get(ch)
        if st == _CSI:
            self._seq += ch
            if not ("@" <= ch <= "~"):
                if len(self._seq) > _CSI_MAX:
                    self._state, self._seq = _GROUND, ""
                return None
            seq, self._seq = self._seq, ""
            if seq == "M":            # legacy mouse report: three raw bytes follow
                self._state, self._left = _MOUSE, 3
            elif seq == "200~":       # bracketed paste: text up to ESC [ 201 ~
                self._state = _PASTE
            else:
                self._state = _GROUND
                return _csi_key(seq)
            return None
        if st == _MOUSE:
            self._left -= 1
            if self._left == 0:
                self._state = _GROUND
            return None
        if st == _STRING:
            if ch == "\x07":
                self._state = _GROUND
            elif ch == "\x1b":
                self._state = _STRING_ESC
            return None
        if st == _STRING_ESC:   # ESC \ is ST; any other byte stays in the string
            self._state = _GROUND if ch == "\\" else _STRING
            return None
        # _PASTE: pasted text is not a command, whatever bytes it holds.
        self._seq = (self._seq + ch)[-len(_PASTE_END):]
        if self._seq == _PASTE_END:
            self._state, self._seq = _GROUND, ""
        return None


# Terminal reporting modes the monitor never asks for but can inherit from a
# program that set them on this terminal and exited without clearing them:
# mouse tracking in every encoding (X10/normal/button/any-motion, UTF-8, SGR,
# urxvt), focus in/out, bracketed paste. The monitor turns them off at start so
# the terminal does not generate the reports in the first place; the decoder
# above is what keeps a report that arrives anyway from becoming a key.
REPORTING_OFF = "\033[?9l\033[?1000l\033[?1002l\033[?1003l\033[?1005l\033[?1006l\033[?1015l\033[?1004l\033[?2004l"


class KeyReader:
    """Owns the reader thread and the queue the render loop drains.

    `start()`/`stop()` bracket one `main()` invocation; `get`/`get_nowait`
    mirror `queue.Queue` so the render loop's drain-the-burst logic reads the
    same either way. `recent_input()` is the raw bytes read in the last few
    seconds, for the monitor's exit record: a quit that nobody typed shows up
    there as the report it was decoded out of.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recent: collections.deque[tuple[float, bytes]] = collections.deque(maxlen=64)

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

    def recent_input(self, seconds: float = 5.0) -> bytes:
        cutoff = time.monotonic() - seconds
        return b"".join(chunk for t, chunk in list(self._recent) if t >= cutoff)

    def _reader_loop(self) -> None:
        """Thread body: read whatever stdin holds, decode it, queue the keys.

        Uses os.read() to bypass Python's BufferedReader — without this,
        select.select() misses bytes the buffer has already consumed. With no
        sequence open the 0.2s poll is only so the thread notices `stop`
        promptly; select() returns as soon as a byte is actually available.
        """
        fd = sys.stdin.fileno()
        decoder = KeyDecoder()
        while not self._stop.is_set():
            wait = decoder.wait()
            if not select.select([fd], [], [], 0.2 if wait is None else wait)[0]:
                for key in decoder.timeout() if wait is not None else ():
                    self._queue.put(key)
                continue
            try:
                data = os.read(fd, 1024)
            except OSError:
                return
            if not data:   # EOF: the terminal is gone, nothing more will arrive
                return
            self._recent.append((time.monotonic(), data))
            for key in decoder.feed(data):
                self._queue.put(key)
