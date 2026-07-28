#!/usr/bin/env python3
"""Resolve a run log plus its rotated archives into ONE oldest-first sequence.

`pipeline.py` rolls `pipeline_anyhow.log` to `.1`, `.2`, … (`.1` newest), so
the full history is spread across N files. Every cross-run reader wants the
same thing — all of it, oldest line first — and getting the order wrong
silently inverts a trend, so it is resolved here once rather than at each
call site.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.log_rotation import archived_logs  # noqa: E402


def log_sequence(log: Path) -> list[Path]:
    """`log` and its archives, OLDEST first. Missing files are skipped."""
    # archived_logs yields newest-first (.1, .2, …) → reversed is oldest-first,
    # and the active file is newer than every archive, so it goes last.
    ordered = [Path(p) for p in reversed(archived_logs(str(log)))]
    if log.exists():
        ordered.append(log)
    return [p for p in ordered if p.exists()]


def read_all(log: Path) -> list[str]:
    """Every line of `log` + its archives, oldest first."""
    out: list[str] = []
    for path in log_sequence(log):
        try:
            out.extend(path.read_text(
                encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return out
