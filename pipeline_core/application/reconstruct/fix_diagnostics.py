"""Fix-loop compile diagnostics — small, reusable units extracted from the
`generate_fix_loop` monolith (P3 slice 1).

`error_sigs`/`errors_by_file` parse CARGO OUTPUT (legitimate regex, R7). `file_stats`
reads source files for the per-file overview table; its fn-decl line scan is the
structured `rust_idents.is_fn_decl_line` (P7 source-regex→tokeniser, byte-equivalent
to the old `_FN_DECL_RE`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pipeline_core.domain.rust_idents import is_fn_decl_line
from pipeline_core.domain.test_results import parse_failed_tests

# A single compile error outranks any plausible failing-test count, so the
# combined metric stays monotonic across the compile→test transition.
_COMPILE_WEIGHT = 10_000

_ERROR_SIG_RE = re.compile(r"error\[E\d+\]: .+")
_ERROR_HEAD_RE = re.compile(r"^error\[E\d+\]")
_ERROR_LOC_RE = re.compile(r"\s+-->\s+(\S+\.rs):\d+")


def error_sigs(output: str) -> frozenset[str]:
    """The distinct `error[Exxxx]: …` signature lines in rustc output."""
    return frozenset(_ERROR_SIG_RE.findall(output))


def floor_metric(output: str) -> int:
    """Lower-is-better fix-loop floor metric spanning BOTH repair phases.

    While the code fails to COMPILE, compile-error signatures dominate (weighted
    so any error outranks any test count); once it compiles (0 sigs) the metric
    becomes the FAILING-TEST count. Without the test term a test-run check sits
    at 0 forever (no compile errors) so the floor-stall can't sense behavioral
    repair — it burns the full stall budget every round. Compile-only output
    (no `test … FAILED`/`---- … stdout ----` lines) → test term 0 → the ordering
    of a build-check loop is unchanged (just scaled)."""
    return len(error_sigs(output)) * _COMPILE_WEIGHT + len(parse_failed_tests(output))


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
        fns = sum(1 for ln in text.splitlines() if is_fn_decl_line(ln))
        stats[rel] = {"lines": lines, "fns": fns}
    return stats
