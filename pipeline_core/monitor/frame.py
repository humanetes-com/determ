"""The monitor's frame geometry: boxes, scroll windows and the split layout.

Every function here takes the width it renders at as an argument and
returns rows; nothing reads the terminal, a module global or an output
buffer. That is the contract the split layout needs: the same box is drawn
at the terminal's width in the tabbed layout and at a column's width in the
split one, and two columns are composed from two row lists.

It replaces three shapes the split layout (2026-09-18) had grown in
`monitor.py`: a module-global width override that every panel read through
`_w()` while one column rendered, a capture of the shared output buffer to
get one column's rows back, and two copies of the scrolling box (log and
events) that differed only in their line source. The box frame is one
convention for every box — content is `width - 4` visible columns between
`│ ` and ` │` — so boxes stacked in one column line up.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from pipeline_core.monitor.render import ansi_seq_len, bold, dim, plain

__all__ = [
    "SplitLayout",
    "box",
    "content_width",
    "fit",
    "scroll_box",
    "side_by_side",
    "tail_lines",
    "truncate_visible",
    "wrap_visible",
]

_RESET = "\033[0m"


def content_width(width: int) -> int:
    """Visible columns between a box's `│ ` and ` │` at outer `width`."""
    return max(0, width - 4)


def truncate_visible(s: str, max_visible: int) -> str:
    """`s` cut to at most `max_visible` visible columns, escapes kept whole
    and a reset appended when anything was cut."""
    if max_visible <= 0:
        return ""
    if len(plain(s)) <= max_visible:
        return s
    out: list[str] = []
    visible = i = 0
    while i < len(s) and visible < max_visible:
        if s[i] == "\033":
            seq = ansi_seq_len(s, i)
            if seq:
                out.append(s[i:i + seq])
                i += seq
                continue
        out.append(s[i])
        visible += 1
        i += 1
    out.append(_RESET)
    return "".join(out)


def fit(line: str, width: int) -> str:
    """`line` padded or cut to exactly `width` visible columns."""
    visible = len(plain(line))
    if visible > width:
        return truncate_visible(line, width)
    return line + " " * (width - visible)


def wrap_visible(s: str, width: int) -> list[str]:
    """`s` split into rows of at most `width` VISIBLE columns.

    Counts only printable characters, so an escape sequence is never cut in
    half (the wrap it replaces sliced the formatted string by raw index and
    could split `\\033[32m` across two rows, colouring the rest of the box).
    Each continuation row re-opens the escapes active at the cut."""
    width = max(1, width)
    if len(plain(s)) <= width:
        return [s]
    rows: list[str] = []
    cur: list[str] = []
    active: list[str] = []
    visible = i = 0
    while i < len(s):
        if s[i] == "\033":
            seq = ansi_seq_len(s, i)
            if seq:
                code = s[i:i + seq]
                cur.append(code)
                active = [] if code == _RESET else [*active, code]
                i += seq
                continue
        if visible == width:
            rows.append("".join(cur) + (_RESET if active else ""))
            cur, visible = list(active), 0
        cur.append(s[i])
        visible += 1
        i += 1
    if visible or not rows:
        rows.append("".join(cur))
    return rows


def box(title: str, rows: Iterable[str], width: int, *,
        status: str = "", height: int | None = None) -> list[str]:
    """A titled box exactly `width` columns wide. Rows wider than the content
    are cut with `…`; `height` pads the content to that many rows. `status`
    is dim text after the title (a scroll position)."""
    inner = content_width(width)
    head = f" {status}" if status else ""
    dashes = max(0, width - len(plain(title)) - len(head) - 5)
    out = [f"┌─ {bold(title)}{dim(head) if head else ''} {'─' * dashes}┐"]
    n = 0
    for row in rows:
        if len(plain(row)) > inner:
            row = truncate_visible(row, max(0, inner - 1)) + "…"
        out.append(f"│ {fit(row, inner)} │")
        n += 1
    for _ in range(max(0, (height or 0) - n)):
        out.append(f"│ {' ' * inner} │")
    out.append("└" + "─" * max(0, width - 2) + "┘")
    return out


def scroll_box(title: str, lines: Sequence[str], width: int, height: int, *,
               offset: int, header: Sequence[str] = ()) -> tuple[list[str], int]:
    """A box of exactly `height` content rows showing `lines` scrolled up by
    `offset` from the bottom, below fixed `header` rows. Returns the rows
    and the offset clamped to what `lines` can scroll (the caller stores it
    back, so a scroll past the top does not accumulate)."""
    region = max(1, height - len(header))
    total = len(lines)
    offset = max(0, min(offset, total - region))
    end = total - offset
    view = list(lines[max(0, end - region):end])
    status = ""
    if total > region:
        pct = int(100 * end / total)
        status = f"{pct}%  end" if offset == 0 else f"{pct}%  ↑{offset}"
    return box(title, [*header, *view], width, status=status,
               height=len(header) + region), offset


def side_by_side(left: Sequence[str], right: Sequence[str],
                 left_w: int, right_w: int, height: int) -> list[str]:
    """Two columns, each row fitted to its column, one blank column between."""
    return [f"{fit(left[i] if i < len(left) else '', left_w)} "
            f"{fit(right[i] if i < len(right) else '', right_w)}"
            for i in range(height)]


def tail_lines(path: str, tail_bytes: int) -> list[str]:
    """The last `tail_bytes` of a text file as lines (a cut first line
    dropped). Raises OSError as `open` does."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - tail_bytes))
        raw = fh.read().decode("utf-8", errors="replace").splitlines()
    return raw[1:] if size > tail_bytes else raw


@dataclass(frozen=True, slots=True)
class SplitLayout:
    """Overview in a narrow left column, a detail view in the wide right one.

    ``left`` is the requested left width (0 = the cap). The left column never
    exceeds ``max_left_pct`` of the screen or goes under ``min_left``; the
    right needs ``min_right``. When both cannot fit the layout is off for
    that frame and the monitor falls back to tabs."""

    enabled: bool = False
    left: int = 0
    max_left_pct: int = 30
    min_left: int = 40
    min_right: int = 60
    step: int = 10

    def _cap(self, total: int) -> int:
        return total * self.max_left_pct // 100

    def widths(self, total: int) -> tuple[int, int] | None:
        """(left, right) at terminal width `total`, or None when off / too narrow."""
        if not self.enabled:
            return None
        cap = self._cap(total)
        left = min(self.left or cap, cap)
        right = total - left - 1
        if left < self.min_left or right < self.min_right:
            return None
        return left, right

    def min_total(self) -> int:
        """The narrowest terminal both columns fit in at the default width."""
        total = self.min_left + self.min_right + 1
        while SplitLayout(True, 0, self.max_left_pct, self.min_left,
                          self.min_right).widths(total) is None:
            total += 1
        return total

    def toggled(self) -> SplitLayout:
        return replace(self, enabled=not self.enabled)

    def resized(self, direction: int, total: int) -> SplitLayout:
        """The left column one `step` wider (direction > 0) or narrower."""
        cap = self._cap(total)
        current = min(self.left or cap, cap)
        step = self.step if direction > 0 else -self.step
        return replace(self, left=max(self.min_left, min(current + step, cap)))
