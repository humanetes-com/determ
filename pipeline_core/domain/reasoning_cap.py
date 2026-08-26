"""Fold of `llm.stream_meta` over one fill window.

`LLMController.run` already emits `finish_reason` / `stop` / `followup` /
`reasoning_chars` per reply. Nothing folded them, so a constructor call that
hit `finish_reason=length` at 67 376 reasoning chars (run `2932-cf516410`,
2026-08-26, 5 of 9 fill replies) was only visible by grepping the event
log after the fact. The count belongs beside the landed-change audit —
replies do not exist yet on the pre-fill `iter N: cargo test` line.

A larger thinking budget is a later, GPU-owned decision. This module only
COUNTS. The live caveat from that constructor trace: diagnosis arrived in
the first screen and extra tokens drifted, so a zero length-hit count is
not the reading this exists to take.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

_active: list[dict[str, Any]] | None = None


def is_reasoning_capped(meta: Mapping[str, Any]) -> bool:
    """A reply that ended because thinking ran out, not because the model
    stopped. `followup` is the recovery that fires on `length` /
    `think_cap`; counting it too matches the 2932 bar (5 of 9)."""
    return (str(meta.get("finish_reason") or "") == "length"
            or str(meta.get("stop") or "") == "think_cap"
            or bool(meta.get("followup")))


def summarize_capped(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """`(capped, total)` over a fill window's `llm.stream_meta` rows."""
    return sum(1 for r in rows if is_reasoning_capped(r)), len(rows)


def record_stream_meta(data: Mapping[str, Any]) -> None:
    """No-op outside :func:`collect_stream_meta`. Append-only; CPython
    `list.append` is atomic, so a worker-thread fill records into the same
    window the iteration opened."""
    active = _active
    if active is not None:
        active.append(dict(data))


@contextmanager
def collect_stream_meta() -> Iterator[list[dict[str, Any]]]:
    """Bind a row list for the duration of one fill pass."""
    global _active
    rows: list[dict[str, Any]] = []
    prev = _active
    _active = rows
    try:
        yield rows
    finally:
        _active = prev
