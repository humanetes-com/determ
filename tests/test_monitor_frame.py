"""The monitor's frame geometry (`pipeline_core.monitor.frame`), and the
monitor frame built on it.

The split layout (2026-09-18) sized every box through a module-global width
override, captured one column's rows out of the shared output buffer, kept
two copies of the scrolling box, and wrapped formatted event lines by raw
string index, which can cut an escape sequence in half. These pin the
contracts that replaced those: every row of a box is exactly its width,
whatever its content; a wrapped row never carries a partial escape; the
scroll offset is clamped; the split bounds and the narrowest terminal they
fit are one object's; and PgUp moves by the height of the box actually drawn.
"""
from __future__ import annotations

import re

import monitor as m
from pipeline_core.monitor.frame import (
    SplitLayout,
    box,
    scroll_box,
    side_by_side,
    wrap_visible,
)
from pipeline_core.monitor.render import green, plain

_PARTIAL_ESCAPE = re.compile(r"\033(?!\[[0-9;]*[A-Za-z])")


def test_every_box_row_is_exactly_the_box_width() -> None:
    rows = box("Title", ["short", "x" * 500, green("coloured " * 40)], 50,
               status="12%  end", height=6)
    assert {len(plain(r)) for r in rows} == {50}
    assert len(rows) == 6 + 2
    assert plain(rows[2]).endswith("… │")


def test_wrap_never_cuts_an_escape_sequence() -> None:
    line = " ".join(green(f"word{i}") for i in range(40))
    rows = wrap_visible(line, 17)
    assert all(len(plain(r)) <= 17 for r in rows)
    assert "".join(plain(r) for r in rows) == plain(line)
    assert not any(_PARTIAL_ESCAPE.search(r) for r in rows)
    # Control: the raw-index slicing it replaced leaves partial escapes.
    naive = [line[i:i + 17] for i in range(0, len(line), 17)]
    assert any(_PARTIAL_ESCAPE.search(r) or r.endswith("\033") for r in naive)


def test_scroll_box_clamps_the_offset_and_shows_the_window() -> None:
    lines = [f"line {i}" for i in range(100)]
    rows, offset = scroll_box("Log", lines, 40, 10, offset=1000, header=["hdr"])
    assert offset == 100 - 9
    body = [plain(r) for r in rows[1:-1]]
    assert body[0].startswith("│ hdr") and body[1].startswith("│ line 0 ")
    rows, offset = scroll_box("Log", lines, 40, 10, offset=0)
    assert offset == 0 and plain(rows[-2]).startswith("│ line 99")
    assert "100%  end" in plain(rows[0])


def test_split_bounds_and_the_narrowest_terminal() -> None:
    layout = SplitLayout(enabled=True)
    assert layout.min_total() == 134
    assert layout.widths(133) is None and layout.widths(134) == (40, 93)
    assert SplitLayout(enabled=False).widths(300) is None
    wide = layout.resized(1, 300)
    assert wide.left == 90                     # already at the 30% cap
    narrow = layout.resized(-1, 300).resized(-1, 300)
    assert narrow.left == 70
    for _ in range(10):
        narrow = narrow.resized(-1, 300)
    assert narrow.left == 40                   # never under the minimum
    assert side_by_side(["a"], ["b", "c"], 3, 2, 2) == ["a   b ", "    c "]


def test_split_frame_columns_line_up_and_panels_get_the_left_width(monkeypatch) -> None:
    widths_seen: list[int] = []
    monkeypatch.setattr(m, "_term_w", lambda: 200)
    monkeypatch.setattr(m, "_h", lambda: 30)
    monkeypatch.setattr(m, "_layout", SplitLayout(enabled=True))
    monkeypatch.setattr(m, "_view", "monitor")
    monkeypatch.setattr(m, "_last_detail", "legend")
    monkeypatch.setattr(m, "gpu", lambda: ["gpu"])
    monkeypatch.setattr(m, "pipeline_status",
                        lambda width: widths_seen.append(width) or ["p" * 300])
    monkeypatch.setattr(m, "generation",
                        lambda width: widths_seen.append(width) or ["g"])
    frame = m._frame(200, 30)
    assert widths_seen == [60, 60]
    body = [plain(r) for r in frame[:-m.BOTTOM_LINES]]
    assert {len(r) for r in body} == {200}
    # Every box edge of the left column sits in the same column.
    assert all(r[59] in "┐│┘ " for r in body)


def test_page_keys_move_by_the_drawn_box(monkeypatch) -> None:
    monkeypatch.setattr(m, "_term_w", lambda: 100)
    monkeypatch.setattr(m, "_layout", SplitLayout())
    monkeypatch.setattr(m, "_view", "pipeline")
    monkeypatch.setattr(m, "_scroll", dict(m._scroll, pipeline=0))
    m._scroll_detail("pipeline", "Log", [str(i) for i in range(500)], 100, 23,
                     header=["h"])
    assert m._page == 22
    m._apply_key("PGUP")
    assert m._scroll["pipeline"] == 22
    m._apply_key("DOWN")
    assert m._scroll["pipeline"] == 22 - m.SCROLL_STEP
