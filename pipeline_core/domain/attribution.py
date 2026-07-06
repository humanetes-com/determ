"""Domain types + pure helper for failure→fn attribution (P2).

`FailureKind` is a StrEnum (its members ARE their wire strings, so existing callers that
treat the kind as a plain string keep working). `AttributionResult` is a NamedTuple so it
still unpacks as `frontier, kind = …` at every call site. `line_to_fn_record` is the pure
(text, line) → fn-record mapping, moved out of pipeline.py.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, NamedTuple

from pipeline_core.domain.models import FnLocator, Frontier


class FailureKind(StrEnum):
    """Which attribution path produced a frontier (for logs/events)."""

    RUNTIME = "runtime"
    COMPILE = "compile"
    BEHAVIOR = "behavior"
    CLASSIFIER = "classifier"
    RECENTLY_FILLED = "recently_filled"
    REVERSE_MAP = "reverse_map"
    MERGED = "merged"
    NO_FRONTIER = "no_frontier"


class AttributionResult(NamedTuple):
    """(frontier, kind). Unpacks as a 2-tuple — backward compatible with the
    `frontier, kind = _attribute_failures(...)` call sites."""

    frontier: Frontier
    kind: FailureKind


def line_to_fn_record(
    file_text: str, line_no: int, fn_records: list[dict[str, Any]],
    col_no: int | None = None,
) -> dict[str, Any] | None:
    """Given file text and a 1-based (line, col), return the fn record whose body
    spans that position, else None. `fn_records` must already be filtered to the
    file. The extractor emits compact one-line-per-item output, so a line can hold
    several fns; `col_no` (when present) disambiguates, otherwise the first fn whose
    body range overlaps the line wins."""
    if line_no < 1 or not fn_records:
        return None
    lines_iter = file_text.splitlines(keepends=True)
    if line_no > len(lines_iter):
        return None
    line_start = sum(len(ln) for ln in lines_iter[:line_no - 1])
    line_end = line_start + len(lines_iter[line_no - 1])
    if col_no is not None and col_no >= 1:
        byte_offset = line_start + col_no - 1
        for rec in fn_records:
            s = rec.get("sig_byte_start", 0)
            e = rec.get("body_byte_end", 0)
            if s <= byte_offset < e:
                return rec
    # Fallback: pick the first fn whose body overlaps the line range.
    for rec in fn_records:
        s = rec.get("sig_byte_start", 0)
        e = rec.get("body_byte_end", 0)
        if max(s, line_start) < min(e, line_end):
            return rec
    return None


def union_unattributed_stubs(
    frontier: Frontier, remaining_stubs: list[FnLocator],
) -> Frontier:
    """Add every remaining `unimplemented!()` stub to the frontier so a helper
    that NO failing test attributes directly (a leaf called only transitively,
    e.g. inside another fn's test) still gets filled in phase-c.

    Without this, an un-attributed stub survives phase-c entirely — it never
    enters the test→fn frontier, so it's only filled by the post-TASK3
    whole-file sweep, while its tested callers thrash as "blocked by an unfilled
    sibling stub" until the cluster plateaus. An un-attributed stub carries an
    EMPTY test set (the u2 gate then accepts it on compile; the outer TDD loop
    re-tests). Already-attributed keys keep their test set unchanged. Returns a
    new dict; inputs are not mutated."""
    merged: Frontier = {key: set(tests) for key, tests in frontier.items()}
    for key in remaining_stubs:
        merged.setdefault(key, set())
    return merged
