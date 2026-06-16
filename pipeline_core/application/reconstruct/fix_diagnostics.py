"""Fix-loop compile diagnostics — small, reusable units extracted from the
`generate_fix_loop` monolith (P3 slice 1).

`error_sigs`/`errors_by_file` parse CARGO OUTPUT (legitimate regex, R7). `file_stats`
reads source files for the per-file overview table; its `fn …` line scan is a text
heuristic over source (R7 migrate-to-syn = P7). Kept verbatim from the closures.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ERROR_SIG_RE = re.compile(r"error\[E\d+\]: .+")
_ERROR_HEAD_RE = re.compile(r"^error\[E\d+\]")
_ERROR_LOC_RE = re.compile(r"\s+-->\s+(\S+\.rs):\d+")
_FN_DECL_RE = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+\w")


def error_sigs(output: str) -> frozenset[str]:
    """The distinct `error[Exxxx]: …` signature lines in rustc output."""
    return frozenset(_ERROR_SIG_RE.findall(output))


def errors_by_file(output: str) -> dict[str, int]:
    """Count primary error locations per file from rustc output."""
    counts: dict[str, int] = {}
    in_error = False
    for line in output.splitlines():
        if _ERROR_HEAD_RE.match(line):
            in_error = True
        elif in_error:
            m = _ERROR_LOC_RE.match(line)
            if m:
                f = m.group(1).lstrip("./")
                counts[f] = counts.get(f, 0) + 1
                in_error = False
    return counts


def file_stats(paths: list[Path], root: Path) -> dict[str, dict[str, Any]]:
    """Lines + fn count per file, keyed by path relative to `root`."""
    stats: dict[str, dict[str, Any]] = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        lines = text.count("\n") + 1
        fns = sum(1 for ln in text.splitlines() if _FN_DECL_RE.match(ln))
        stats[rel] = {"lines": lines, "fns": fns}
    return stats
