"""Run-log rotation — keep a log file bounded by dropping its head. Filesystem
adapter relocated from pipeline.py (C1). Fail-safe: any I/O error is swallowed
so log housekeeping never aborts a run."""
from __future__ import annotations

import os


def trim_head(path: str, max_bytes: int) -> None:
    """Truncate ``path`` to its last ``max_bytes``, cutting at the first newline
    boundary so the file stays line-aligned. No-op when already small enough."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "r+b") as fh:
            fh.seek(-max_bytes, 2)
            tail = fh.read()
            nl = tail.find(b"\n")
            if nl >= 0:
                tail = tail[nl + 1:]
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
    except Exception:
        pass
