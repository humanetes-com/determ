"""Drawing the phase board: the strip, the sub-step row and the detailed view.

Pure text in, text out; every state reading comes from
:mod:`pipeline_core.monitor.phase_board` and every word of description from
:mod:`pipeline_core.domain.phase_registry`. Same grammar as the iteration step
row it sits above (✓ done, ▶ current, · pending), plus ↷ for a phase whose own
checkpoint said it was already done, ✗ for an error and ‖ for a phase the
process left unfinished.

Three pieces:

* :func:`strip_rows` — tier one, the top-level strip, and tier two, the active
  phase's sub-steps with a per-cluster status when it runs per cluster and the
  time in the current step, ending in the `[e]` hint. Never wraps: when the
  strip does not fit, the leading cells collapse to `…`.
* :func:`detail_lines` — the `[e]` view: one block per phase with what it does,
  why it exists, its steps with live status and durations, what it reads and
  writes and when it is skipped. The running phase, and every phase not yet
  reached, is expanded; finished phases are one line unless `expand_all`.
* :func:`detail_start` — the row the detailed view opens scrolled to: the block
  of the running phase.
"""
from __future__ import annotations

import textwrap
from collections.abc import Sequence

from pipeline_core.domain.phase_registry import TOP_LEVEL, Phase, children_of
from pipeline_core.monitor.phase_board import (
    Board,
    PhaseStatus,
    Slot,
    focus_top,
    phase_status,
)
from pipeline_core.monitor.render import cyan, dim, green, plain, red, yellow

__all__ = ["strip_rows", "detail_lines", "detail_start", "HINT"]

HINT = "[e] explain"

_GLYPH = {"done": "✓", "running": "▶", "pending": "·", "skipped": "↷",
          "error": "✗", "aborted": "‖"}
_PAINT = {"done": green, "running": cyan, "pending": dim, "skipped": dim,
          "error": red, "aborted": yellow}
_WORD = {"done": "done", "running": "running", "pending": "pending",
         "skipped": "skipped — already done", "error": "failed",
         "aborted": "stopped before finishing"}


def _cell(status: str, label: str) -> str:
    return _PAINT[status](_GLYPH[status] + label)


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds / 3600:.1f}h"


def _fit(cells: Sequence[str], width: int, prefix: str, tail: str = "",
         focus: int | None = None) -> str:
    """`prefix` + cells (+ tail) within `width` visible columns. The tail goes
    first, then cells are dropped from the side the focused cell is further
    from (`…` marks each side that was cut) until it fits. The focused cell —
    the running phase or step — is never dropped, so it stays on screen at any
    width; with no focus the last cell is kept."""
    cells = list(cells)
    cur = len(cells) - 1 if focus is None else focus
    lo, hi = 0, len(cells)

    def render(t: str) -> str:
        lead = dim("… ") if lo > 0 else ""
        trail = dim(" …") if hi < len(cells) else ""
        return prefix + lead + " ".join(cells[lo:hi]) + trail + t

    text = render(tail)
    if len(plain(text)) <= width:
        return text
    while len(plain(render(""))) > width and hi - lo > 1:
        if cur - lo >= hi - 1 - cur and lo < cur:
            lo += 1
        elif hi - 1 > cur:
            hi -= 1
        else:
            lo += 1
    return render("")


def _step_slot(board: Board, child_id: str, cluster: str) -> Slot | None:
    return board.slots.get((child_id, cluster)) or board.slots.get((child_id, ""))


def _active_cluster(board: Board, top: Phase, st: PhaseStatus) -> str:
    """The cluster the sub-step row describes: the running one, else the last
    one with any slot. A phase that is not itself per cluster (the bake) can
    still have per-cluster steps: then it is the cluster of the latest step."""
    for cl, s in st.clusters.items():
        if s == "running":
            return cl
    seen = [cl for cl, s in st.clusters.items() if s != "pending"]
    if seen:
        return seen[-1]
    kid_ids = {c.id for c in children_of(top.id)}
    for (pid, cl), _slot in reversed(list(board.slots.items())):
        if pid in kid_ids and cl:
            return cl
    return ""


def _child_clusters(board: Board, c: Phase, st: PhaseStatus) -> list[str]:
    """Which clusters a per-cluster step is listed under in the detailed view."""
    if not c.per_cluster:
        return [""]
    if st.clusters:
        return list(st.clusters)
    seen = [cl for (pid, cl) in board.slots if pid == c.id and cl]
    ordered = list(board.clusters) + [cl for cl in seen if cl not in board.clusters]
    return ordered or [""]


def strip_rows(board: Board, width: int, now: float) -> list[str]:
    """Tier one and tier two (see the module docstring)."""
    cells = []
    focus_idx: int | None = None
    for i, p in enumerate(TOP_LEVEL):
        status = phase_status(board, p).status
        cells.append(_cell(status, p.label))
        if status in ("running", "aborted", "error"):
            focus_idx = i
    top = focus_top(board)
    if focus_idx is None and top is not None:
        focus_idx = next(i for i, p in enumerate(TOP_LEVEL) if p.id == top.id)
    rows = [_fit(cells, width, "  phase    ", focus=focus_idx)]

    if top is None:
        rows.append("  " + dim("waiting for the first phase") + "  " + dim(HINT))
        return rows
    st = phase_status(board, top)
    label = top.label.ljust(9)
    kids = children_of(top.id)
    active = _active_cluster(board, top, st)
    parts: list[str] = []
    if top.per_cluster and len(st.clusters) > 1:
        parts.append("  ".join(
            _PAINT[s](f"{cl} {_GLYPH[s]}") for cl, s in st.clusters.items()))
        parts.append(dim("│"))
    running_child: tuple[str, float | None] | None = None
    child_focus: int | None = None
    for c in kids:
        slot = _step_slot(board, c.id, active)
        status = slot.status if slot else "pending"
        if not c.per_cluster and slot is None:
            slot = _step_slot(board, c.id, "")
            status = slot.status if slot else "pending"
        parts.append(_cell(status, c.label))
        if status == "running" and slot is not None:
            running_child = (c.label, (now - slot.since) if slot.since else None)
            child_focus = len(parts) - 1
    if not kids:
        parts.append(_PAINT[st.status](f"{_GLYPH[st.status]}{top.title.lower()}"))
    tail = ""
    if st.status == "running":
        if running_child is not None and running_child[1] is not None:
            tail = dim(f"  {_dur(running_child[1])} in {running_child[0]}")
        elif st.since is not None:
            tail = dim(f"  {_dur(now - st.since)}")
    elif st.status in ("aborted", "error"):
        tail = _PAINT[st.status](f"  {_WORD[st.status]}")
    rows.append(_fit(parts, width, "  " + label, tail + "  " + dim(HINT),
                     focus=child_focus))
    return rows


# ---------------------------------------------------------------- detail

def _wrap(text: str, width: int, indent: str, first: str | None = None) -> list[str]:
    out = textwrap.wrap(text, max(20, width - len(indent)),
                        initial_indent=first if first is not None else indent,
                        subsequent_indent=indent)
    return out or [indent]


def _phase_time(p: Phase, st: PhaseStatus, now: float) -> str:
    if st.status == "running" and st.since is not None:
        return _dur(now - st.since)
    return _dur(st.duration_s)


def _clusters_text(st: PhaseStatus) -> str:
    return "  ".join(f"{cl} {_GLYPH[s]}" for cl, s in st.clusters.items())


def _step_lines(board: Board, p: Phase, st: PhaseStatus, now: float,
                width: int) -> list[str]:
    out: list[str] = []
    for c in children_of(p.id):
        for cl in _child_clusters(board, c, st):
            slot = _step_slot(board, c.id, cl)
            status = slot.status if slot else "pending"
            when = ""
            if slot is not None:
                when = (_dur(now - slot.since) if status == "running" and slot.since
                        else _dur(slot.duration_s))
            where = f" [{cl}]" if cl else ""
            head = _cell(status, f" {c.title}{where}")
            note = f"  {slot.detail}" if slot is not None and slot.detail else ""
            out.append("      " + head + (dim(f"   {when}") if when else "")
                       + dim(note))
        out.extend(dim(ln) for ln in _wrap(c.what, width, "          "))
    return out


def _block(board: Board, p: Phase, width: int, now: float, *,
           expanded: bool) -> list[str]:
    st = phase_status(board, p)
    clusters = _clusters_text(st) if p.per_cluster else ""
    head = _PAINT[st.status](f"{_GLYPH[st.status]} {p.label}") + "  " + p.title
    right = dim(_WORD[st.status] + (f"  {_phase_time(p, st, now)}"
                                    if _phase_time(p, st, now) else ""))
    line = f"  {head}   {right}"
    if clusters:
        line += "   " + dim(clusters)
    if not expanded:
        return [line]
    out = [line]
    out.extend(_wrap(p.what, width, "      "))
    if p.why:
        out.extend(_wrap(p.why, width, "        ", first="      why: "))
    kids = _step_lines(board, p, st, now, width)
    if kids:
        out.append("      " + dim("steps:"))
        out.extend(kids)
    for label, text in (("reads", p.reads), ("writes", p.writes),
                        ("skipped when", p.skipped_when)):
        if text:
            out.extend(_wrap(text, width, "        ", first=f"      {label}: "))
    out.append("")
    return out


def _is_expanded(status: str, expand_all: bool) -> bool:
    return expand_all or status in ("running", "pending", "error", "aborted")


def detail_lines(board: Board, width: int, now: float, *,
                 expand_all: bool = False) -> list[str]:
    """The `[e]` view's rows. Every top-level phase, in run order."""
    lines: list[str] = []
    for p in TOP_LEVEL:
        status = phase_status(board, p).status
        lines.extend(_block(board, p, width, now,
                            expanded=_is_expanded(status, expand_all)))
    return lines


def detail_start(board: Board, width: int, now: float, *,
                 expand_all: bool = False) -> int:
    """Row index of the block the view opens on: the running phase, else the
    phase the last event belongs to, else the first."""
    top = focus_top(board)
    row = 0
    for p in TOP_LEVEL:
        status = phase_status(board, p).status
        if top is not None and p.id == top.id:
            return row
        row += len(_block(board, p, width, now,
                          expanded=_is_expanded(status, expand_all)))
    return 0
