"""File IO helpers — read/write a text file by path (ambient infra).

Relocated verbatim from pipeline.py (slice 5b foundation). Trivial fs primitives used
across the pipeline; given a home so callers import them instead of redefining. `read`
swallows FileNotFoundError (returns "") and `write` creates parent dirs — the long-standing
pipeline semantics, preserved exactly.

Tasks that want to control reads/writes under test still inject these (DIP/testability);
non-test code imports them here.
"""

from __future__ import annotations

from pathlib import Path


def read(path) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def write(path, content):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)
