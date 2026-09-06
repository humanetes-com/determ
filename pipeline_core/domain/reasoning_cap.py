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

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_active: list[dict[str, Any]] | None = None

# The units (`file::fn`) the call currently in flight is serving, bound by
# the fill route around its ask so the controller's row lands already
# joined. A ContextVar because fill routes run on worker threads: each
# thread's binding scopes its own calls while every row still appends into
# the one window the iteration opened. Empty = an unjoined call (a
# retrospective, a classifier), whose rows no per-unit fold reads.
_units_var: ContextVar[tuple[str, ...]] = ContextVar(
    "reasoning_cap_units", default=())

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


def is_truncated_followup(row: Mapping[str, Any]) -> bool:
    """A followup row whose reply is INCOMPLETE, by either mechanism.

    Two of them, and they are not the same event: the server ran out of
    tokens (`finish_reason == "length"`) or this client stopped reading
    mid-reasoning at the char cap (`stop == "think_cap"`, `_consume` breaks
    the stream). :func:`summarize_followup_cut` counts the first alone and
    keeps that name — a server budget is what sizing `request_max` answers,
    and a client break is not. Salvage has no such distinction:
    `LLMController._followup` salvages on BOTH, because both leave a reply
    whose last element is unfinished, so the fold that reports salvage must
    cover both or it undercounts every think-capped followup. Invisible while
    `PHASE_C_FOLLOWUP_THINK` is off (nothing arms the cap on the followup),
    wrong the moment it is switched on — which is when the reading matters.
    `stop == "closed"` is the StopDetector ending a FINISHED structure and is
    neither."""
    return (_is_followup_row(row)
            and (str(row.get("finish_reason") or "") == "length"
                 or str(row.get("stop") or "") == "think_cap"))


def summarize_followup_cut(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many forced followups in the window ran out of SERVER tokens
    (`finish_reason == "length"`) — the completion that wrote the reply on
    record was itself budget-bound, so the reply is a truncation of a
    truncation. A client-side `think_cap` break is a different event and is
    not counted here (see :func:`is_truncated_followup`). Stream rows are
    ignored: their `finish_reason` is the MAIN stream's, which
    `summarize_capped` already counts."""
    return sum(1 for r in rows
               if _is_followup_row(r) and str(r.get("finish_reason") or "") == "length")


def summarize_followup_salvage(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """`(cut, salvaged)` over the followup rows in the window.

    `cut` is every TRUNCATED followup (:func:`is_truncated_followup` — server
    budget or client think-cap break), which is a wider population than
    :func:`summarize_followup_cut`'s server-budget count, because the
    controller salvages both. `salvaged` is how many of those
    still yielded a structurally closed prefix that was spliced — a cut whose
    damage was the LAST element only. The pair is the reading the discard
    rule needs: `cut` alone cannot tell "the completion budget is too small
    and we are losing whole replies" from "it is too small and we are losing
    one body of twenty-five". A row without `salvaged_chars` (written before
    the field) counts as not salvaged, which is what it was."""
    truncated = sum(1 for r in rows if is_truncated_followup(r))
    salvaged = sum(1 for r in rows
                   if is_truncated_followup(r) and int(r.get("salvaged_chars") or 0) > 0)
    return truncated, salvaged


def current_units() -> tuple[str, ...]:
    """The `file::fn` labels bound by the innermost `serving_units`; empty
    outside one. Read by the fill reading so a mid-fill measurement names
    the unit(s) whose gate took it."""
    return tuple(_units_var.get())


@contextmanager
def serving_units(units: Iterable[str]) -> Iterator[None]:
    """Bind the `file::fn` unit labels the enclosed model call(s) serve.

    Rows recorded inside carry them under `units`, which is the join the
    FLAT grading needs: run `358833-aa15d93c` iter 3 (2026-09-01) wrote 8
    FLAT ledger rows in a batch whose audit line said `3 of 4 fill replies
    reasoning-capped` — the count and the grades lived in the same window
    with no way to say WHICH units' tries were the truncated ones."""
    token = _units_var.set(tuple(dict.fromkeys(units)))
    try:
        yield
    finally:
        _units_var.reset(token)


def _record(data: Mapping[str, Any]) -> None:
    active = _active
    if active is not None:
        row = dict(data)
        units = _units_var.get()
        if units:
            row["units"] = units
        active.append(row)


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


def units_all_replies_capped(
    rows: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """The units (`file::fn`) whose EVERY joined main-stream reply in this
    window was reasoning-capped — at least one reply, all of them capped.

    A truncated call that re-emits the old body is not a measured refusal:
    grading such a unit FLAT stands the re-buy detector on a truncated
    measurement, the same overclaim the FLAT arm's own comment forbids for
    reverts. A unit with any UNCAPPED reply keeps its grade — the model got
    a full turn and returned the same body. Unjoined rows (no `units`) and
    followup rows (a second completion of the same call, whose cap
    `is_reasoning_capped` already folds into the main row) count toward no
    unit."""
    total: dict[str, int] = {}
    capped: dict[str, int] = {}
    for r in rows:
        if _is_followup_row(r):
            continue
        for u in r.get("units") or ():
            total[u] = total.get(u, 0) + 1
            if is_reasoning_capped(r):
                capped[u] = capped.get(u, 0) + 1
    return frozenset(u for u, n in total.items() if capped.get(u, 0) == n)


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
