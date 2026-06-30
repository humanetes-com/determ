"""ChunkProgress — per-chunk timing + progress rendering for the parallel
generation loop. Relocated from pipeline.py (C2). Routes its lines through the
`pipeline_core.log` facade; the ``--progress`` terminal mode is injected as a
constructor flag (no run-global read)."""
from __future__ import annotations

import threading
import time

from pipeline_core.log import log


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def render_progress(current: int, total: int, avg_seconds: float | None,
                    bar_width: int = 30) -> str:
    """Render a single-line progress bar string.
    Example: [=====>    ] 45/276 (16%) | avg 2m04s/chunk | ETA ~7h21m"""
    pct = current / total if total else 0
    filled = int(bar_width * pct)
    arrow = "=" * filled + (">" if filled < bar_width else "")
    bar = f"[{arrow:<{bar_width}}]"
    base = f"{bar} {current}/{total} ({pct:.0%})"
    if avg_seconds and avg_seconds > 0:
        remaining = avg_seconds * (total - current)
        base += f" | avg {_fmt_duration(avg_seconds)}/chunk | ETA ~{_fmt_duration(remaining)}"
    return base


class ChunkProgress:
    """Tracks per-chunk timing and emits progress output after each chunk.

    Default mode  : prints a new line per chunk (log-friendly).
    progress mode : overwrites the same terminal line with \\r (clean live view).
    """

    def __init__(self, total: int, rolling_window: int = 5, *,
                 progress_mode: bool = False) -> None:
        self.total = total
        self.window = rolling_window
        self.progress_mode = progress_mode
        self._times: list[float] = []   # recent chunk durations
        self._local = threading.local()  # per-thread start time (concurrent chunks)
        self._lock = threading.Lock()     # guards _times/_last_line_len/print
        self._last_line_len: int = 0    # for \r padding in progress mode

    def start_chunk(self) -> None:
        self._local.chunk_start = time.monotonic()

    def finish_chunk(self, current: int) -> None:
        elapsed = time.monotonic() - getattr(self._local, "chunk_start", time.monotonic())
        with self._lock:
            self._times.append(elapsed)
            if len(self._times) > self.window:
                self._times.pop(0)
            avg = sum(self._times) / len(self._times) if self._times else None

            line = render_progress(current, self.total, avg)

            if self.progress_mode:
                # Overwrite previous line in terminal.
                padded = line.ljust(self._last_line_len)
                print(f"\r{padded}", end="", flush=True)
                self._last_line_len = len(line)
            else:
                # New line — works in log files and when tailing.
                log(f"  {line}")

    def finish(self) -> None:
        """Print a final newline when in progress mode."""
        if self.progress_mode and self._last_line_len:
            print()  # move past the \r line
