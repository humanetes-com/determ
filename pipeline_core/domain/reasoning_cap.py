"""Fold of `llm.stream_meta` / `llm.followup_meta` over one fill window.

`LLMController.run` already emits `finish_reason` / `stop` / `followup` /
`reasoning_chars` per reply. Nothing folded them, so a constructor call that
hit `finish_reason=length` at 67 376 reasoning chars (run `2932-cf516410`,
2026-08-26, 5 of 9 fill replies) was only visible by grepping the event
log after the fact. The count belongs beside the landed-change audit —
replies do not exist yet on the pre-fill `iter N: cargo test` line.

The followup rows are a second kind in the SAME window. A `stream_meta` row
with `followup=True` says the main stream was cut and a forced completion
ran; it says nothing about whether THAT completion finished. Measured
2026-08-26 on run `82157-476ddc16`: all 8 file-first replies were the
followup's, each written under `request_max` = the content budget (8192)
with no thinking reserve, and the row on record carried the main stream's
`finish_reason`. `summarize_followup_cut` is the count that was missing —
followups that were themselves cut at `length`.

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

# Marker key on rows recorded by `record_followup_meta`. Stream rows carry no
# `kind` so their dict stays byte-identical to the `llm.stream_meta` payload.
_KIND = "kind"
_FOLLOWUP = "followup"


def is_reasoning_capped(meta: Mapping[str, Any]) -> bool:
    """A reply that ended because thinking ran out, not because the model
    stopped. `followup` is the recovery that fires on `length` /
    `think_cap`; counting it too matches the 2932 bar (5 of 9)."""
    return (str(meta.get("finish_reason") or "") == "length"
            or str(meta.get("stop") or "") == "think_cap"
            or bool(meta.get("followup")))


def _is_followup_row(row: Mapping[str, Any]) -> bool:
    return str(row.get(_KIND) or "") == _FOLLOWUP


def summarize_capped(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """`(capped, total)` over a fill window's `llm.stream_meta` rows.

    Followup rows share the window and are EXCLUDED from both counts. The
    audit line reads `N of M fill replies reasoning-capped`, where M is one
    row per call; a followup is a second completion of the same call and it
    runs on exactly the calls that were capped, so admitting it would raise
    the denominator by one per capped call and read 5 of 9 as 5 of 14."""
    stream = [r for r in rows if not _is_followup_row(r)]
    return sum(1 for r in stream if is_reasoning_capped(r)), len(stream)


def summarize_followup_cut(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many forced followups in the window were themselves cut at
    `finish_reason == "length"` — the completion that wrote the reply on
    record ran out of tokens too, so the reply is a truncation of a
    truncation. Stream rows are ignored: their `finish_reason` is the MAIN
    stream's, which `summarize_capped` already counts."""
    return sum(1 for r in rows
               if _is_followup_row(r) and str(r.get("finish_reason") or "") == "length")


def _record(data: Mapping[str, Any]) -> None:
    active = _active
    if active is not None:
        active.append(dict(data))


def record_stream_meta(data: Mapping[str, Any]) -> None:
    """No-op outside :func:`collect_stream_meta`. Append-only; CPython
    `list.append` is atomic, so a worker-thread fill records into the same
    window the iteration opened."""
    _record(data)


def record_followup_meta(data: Mapping[str, Any]) -> None:
    """Same window, same atomic append, tagged `kind: followup` so the two
    folds above can tell the forced completion's row from the main
    stream's. The tag is added here rather than by the caller so a row can
    never land untagged and inflate `summarize_capped`'s denominator."""
    _record({**data, _KIND: _FOLLOWUP})


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
